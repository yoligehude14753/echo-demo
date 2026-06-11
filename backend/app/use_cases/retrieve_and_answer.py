"""use_case: retrieve_and_answer — RAG-grounded 问答（取代 PR-2 的 ask_question 朴素版）。

流程：
  1) Fast 通道分类器（Qwen3-1.7B）判别 query 类型：
     - "rag"：本地知识库可答（PDF/会议）
     - "web"：需联网（最新资讯/时事/价格）
     - "either"：两边都试
  2) 按分类执行 RAG / Web / 都跑
  3) 把检索结果拼到 system prompt，用 MAIN 通道（M2.7）流式生成最终答复

约束：
- 仅依赖 ports.LLMPort / ports.RagPort / ports.WebSearchPort
- 检索失败不打断流程：拼空字符串 + 答复中说明"无相关来源"
- fabrication_guard：MAIN prompt 显式要求"未在引用中找到 → 说不知道"

覆盖率目标（2026-05-28 用户原话）：
- 「起码能覆盖 1000 个对话，100 个文件，这还是最低标准」
- 索引规模目标 5k-10k chunks；粗召回 top_k=1000；prompt 渲染 80 chunks
- 80 chunks × 平均 400 token ≈ 32k token，加上 system+query 约 40k input，
  M2.7 80k 窗口预算内安全；留 ~40k 给生成。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.web_search import WebSearchPort
from app.schemas.llm import ChatMessage
from app.schemas.rag import RagChunk, RetrievalResult, WebHit

_CLASSIFIER_PROMPT = """你是路由器。给定一个用户问题，判断它适合从哪里取证据。
只能输出三个标签之一：
- rag：用户的问题与"本地知识库"（已上传的 PDF / 已结束的会议）有关，可在本地检索
- web：用户的问题涉及"最新""今天""目前""新闻""价格""天气"等时效性强的内容，需要联网
- either：模糊或两者都可能

只输出标签，不要解释。"""

_ANSWER_PROMPT_TEMPLATE = """你是 EchoDesk,会议+办公场景的数字分身。

下面是检索到的证据(含本地知识库 RAG、联网 Web、当前会议上下文)。请基于证据回答用户问题。
要求:
1) 简洁分点,中文
2) 引用证据时标 [doc:doc_id-chunk_id] 或 [web:url]
3) 如果证据中找不到答案 → 直接说 "在已有的资料里没找到相关内容",不要编造
4) 不要凭空假设事实
5) 如果"当前会议上下文"里能看出用户和参会人的真实意图,优先以此为准

---- 当前会议上下文(最近转录) ----
{inline_context_block}

{rag_guidance}---- 证据(RAG) ----
{rag_block}

---- 证据(Web) ----
{web_block}

---- 用户问题 ----
{question}
"""


# === 关键参数（rag_redesign_2026-05-28, D-new 规格） ===

_AMBIENT_BM25_PENALTY = 0.4
"""ambient(环境录音转录)类 chunk 的 BM25 score 倍率。

历史:
- 2026-05-28 初版 0.25:让 score≈12 的 ambient 降到 3 左右与 PDF 并列。
- 2026-05-28 D-new 调到 0.4:配合 D.3 b=0.5 + doc-cap=12 + 大召回 + grep boost,
  0.4 已经足够压制 ambient 长文档霸榜,同时保留 "刚才说了啥" 类问题的召回能力。
"""

_DEFAULT_RAG_TOP_K = 1000
"""粗召回 top_k。

用户原话(2026-05-28):「起码能覆盖 1000 个对话,100 个文件,这还是最低标准」。
rank_bm25 get_scores 在 10k chunks 量级 ≈ 50ms,1000 量级完全不是瓶颈。
拉满粗召回,把过滤和排序留给本 use case 层(doc-cap + grep boost + 优先级)。
"""

_PROMPT_RENDER_TOP_N = 80
"""prompt 实际渲染的 chunk 数。

预算:80 chunks × 平均 400 token = 32k token,加 system+query+history 共 ~40k input,
留 ~40k 给生成,M2.7 80k 窗口内安全。
"""

_DOC_CHUNK_CAP = 12
"""同一 doc_id 在 rerank 阶段最多保留的 chunk 数。

避免单 doc(尤其是 ambient daily,实测一天 1236 chunks)霸榜整个 prompt 窗口。
12 这个数对中等规模 PDF 仍然友好(实测 PDF 中位数 11 chunks,基本能完整带出)。
"""

# 标点 + 空白规范化(grep-style 字面匹配用)
_GREP_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)

# 极简停用词表(主要剔除中文虚词,避免污染 keyword 匹配)
_GREP_STOPWORDS = frozenset(
    [
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "及",
        "或",
        "也",
        "都",
        "等",
        "啊",
        "呢",
        "吗",
        "吧",
        "把",
        "被",
        "让",
        "给",
        "对",
        "从",
        "what",
        "where",
        "when",
        "who",
        "why",
        "how",
    ]
)


@dataclass
class AnswerStream:
    """retrieve_and_answer 的产物：检索结果 + 流式答复。"""

    retrieval: RetrievalResult
    chunks: AsyncIterator[str]


# === 重排辅助函数 ===


def _normalize_for_grep(text: str) -> str:
    """去掉所有空白与标点并 lowercase,用于 grep-style 精确子串匹配。

    例: "褐蚁 V2.0 (FY26)" -> "褐蚁v20fy26"
    """
    return _GREP_NORM_RE.sub("", text.lower())


def _extract_keywords(query: str) -> list[str]:
    """jieba.lcut → 过滤单字与停用词 → 返回 keyword 列表(全部 lowercase)。

    用于 grep-style "keyword 多数命中" 判断。注意单字会被过滤(中文单字 IDF 信息
    量低,容易污染匹配)。
    """
    import jieba

    tokens = jieba.lcut(query.lower())
    out: list[str] = []
    for raw in tokens:
        tok = raw.strip()
        if len(tok) < 2 or tok in _GREP_STOPWORDS:
            continue
        out.append(tok)
    return out


def _grep_boost(chunk: RagChunk, norm_query: str, keywords: list[str]) -> float:
    """grep-style 字面提升(D-new, rag_redesign_2026-05-28)。

    - 若 chunk.text 规范化后精确包含 query 整串 → +2.0
      (对 "褐蚁"、"FY26-Q3-XPL" 这种专有名词 query 直接命中)
    - 若包含 query keyword 的 2/3 以上 → +0.5
      (对 query 的不同切法仍能稳健触发)

    两条独立叠加(同时命中 → +2.5)。
    """
    norm_text = _normalize_for_grep(chunk.text)
    boost = 0.0
    if norm_query and norm_query in norm_text:
        boost += 2.0
    if keywords:
        matched = sum(1 for kw in keywords if kw in norm_text)
        if matched / len(keywords) >= 2 / 3:
            boost += 0.5
    return boost


# 「对话类查询」信号:用户在问之前说过/会上聊过/某时段发生的事 → 应优先 ambient/meeting。
_CONVERSATION_QUERY_RE = re.compile(
    r"(上午|下午|早上|晚上|昨天|今天|前天|前几天|前两天|这几天|上次|上回|早些|早前|"
    r"刚才|刚刚|之前|先前|方才|当时|那天|这边|"
    r"会上|会里|会议|开会|纪要|聊到|说到|提到|讲到|谈到|聊过|说过|提过|讲过|"
    r"讨论|谁说|谁讲|谁负责|刚说|刚讲|刚提|对话|聊的|说的|"
    r"遗留|待办|跟进|对接|进展|安排)"
)


def _is_conversation_query(query: str) -> bool:
    """查询是否在问"之前对话/会议里说过的内容"。"""
    return bool(_CONVERSATION_QUERY_RE.search(query))


def _chunk_kind(c: RagChunk) -> str:
    """归一出 chunk 来源:ambient / meeting / doc。"""
    src = c.metadata.get("source", "")
    kind = c.metadata.get("kind", "")
    if src == "ambient" or kind == "ambient":
        return "ambient"
    if kind == "meeting" or src == "meeting":
        return "meeting"
    return "doc"


def _source_nudge(c: RagChunk, *, conv_query: bool) -> float:
    """source 作为**小幅**加权(不再当排序主键)。

    - 对话类查询:ambient/meeting **加分**(用户就是在问对话里说过的内容)。
    - 普通(事实)查询:文档略占优,ambient 略降(避免日常闲聊霸榜),但都是软调,
      不会像以前那样把对话内容硬压到所有文档之后。
    """
    k = _chunk_kind(c)
    if conv_query:
        return {"meeting": 2.0, "ambient": 1.5, "doc": 0.0}[k]
    return {"doc": 0.5, "meeting": 0.0, "ambient": -0.5}[k]


def _adjusted_score(c: RagChunk, *, conv_query: bool) -> float:
    """ambient 削分仅用于"普通事实查询";对话类查询不削(否则查不到对话内容)。"""
    if _chunk_kind(c) == "ambient" and not conv_query:
        return c.score * _AMBIENT_BM25_PENALTY
    return c.score


# ── 时间感知检索：把"上午/昨天/刚才说到 X"里的时间词解析成时间窗 ──────────
# ambient chunk 带 captured_at（UTC ISO）；窗内段加分、窗外 ambient 段降权，
# 让"上午说到 X"这类按时间回忆的查询更精准（不靠时间词时此功能整体不生效）。
_TIME_BOOST_IN_WINDOW = 1.5
_TIME_PENALTY_OUT_WINDOW = 0.8
_RECENT_MINUTES = 40


# 时段词 → (起,止) 小时；按列表顺序取首个命中（表驱动，避免一长串 if 分支）。
_TIME_OF_DAY: tuple[tuple[str, tuple[int, int]], ...] = (
    (r"早上|早晨|一早|清晨", (5, 10)),
    (r"上午", (6, 12)),
    (r"中午|午间", (11, 14)),
    (r"下午", (12, 18)),
    (r"傍晚", (17, 20)),
    (r"晚上|晚间|夜里|半夜|昨晚|今晚", (18, 24)),
)
_DAY_OFFSET: tuple[tuple[str, int], ...] = (
    (r"前天", -2),
    (r"昨天|昨晚|昨日", -1),
    (r"今天|今日|今早|今晚|今儿", 0),
)


def _time_window_from_query(
    query: str, now: datetime
) -> tuple[datetime, datetime] | None:
    """从查询里解析时间窗（本地时区 aware）；无时间词返回 None。"""
    if re.search(r"刚才|刚刚|方才|刚说|刚提|刚讲|这会儿", query):
        return (now - timedelta(minutes=_RECENT_MINUTES), now)

    day_off = next((off for pat, off in _DAY_OFFSET if re.search(pat, query)), None)
    tod = next((hrs for pat, hrs in _TIME_OF_DAY if re.search(pat, query)), None)

    if day_off is None and tod is None:
        return None
    base = (now + timedelta(days=day_off or 0)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if tod is None:
        return (base, base + timedelta(days=1))
    return (base + timedelta(hours=tod[0]), base + timedelta(hours=tod[1]))


def _time_boost(c: RagChunk, window: tuple[datetime, datetime] | None) -> float:
    """按 chunk 的 captured_at 是否落在时间窗内给加/降权。"""
    if window is None:
        return 0.0
    raw = c.metadata.get("captured_at")
    if not raw:
        return 0.0  # 没时间戳（如文档）不参与时间加权
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return 0.0
    start, end = window
    # aware/naive 兼容：captured_at 缺 tz 时按窗口 tz 补齐再比较
    if ts.tzinfo is None and start.tzinfo is not None:
        ts = ts.replace(tzinfo=start.tzinfo)
    return _TIME_BOOST_IN_WINDOW if start <= ts <= end else -_TIME_PENALTY_OUT_WINDOW


def _rerank_diverse_with_priority_and_grep_boost(
    chunks: list[RagChunk], query: str, *, now: datetime | None = None
) -> list[RagChunk]:
    """doc-cap=12 + grep 字面提升 + source **软加权**(相关度才是排序主键)。

    2026-06 修复:以前按 ``(source优先级, score)`` 元组排序,source 当主键 → 不管
    相关度多高,ambient/会议内容永远排在所有文档之后 → 跨对话查询查不到。现改为:
    1) 按 doc_id 分组,每 doc 仅保留分数最高的前 _DOC_CHUNK_CAP=12 chunks
    2) 单一相关度排序:adjusted_score + grep_boost + source_nudge(小幅)
    3) 对话类查询里 ambient/会议获得正向加分,而非被压制
    4) 返回 top-_PROMPT_RENDER_TOP_N=80
    """
    by_doc: dict[str, list[RagChunk]] = defaultdict(list)
    for c in chunks:
        by_doc[c.doc_id].append(c)

    capped: list[RagChunk] = []
    for doc_chunks in by_doc.values():
        doc_sorted = sorted(doc_chunks, key=lambda x: x.score, reverse=True)
        capped.extend(doc_sorted[:_DOC_CHUNK_CAP])

    norm_query = _normalize_for_grep(query)
    keywords = _extract_keywords(query)
    conv_query = _is_conversation_query(query)
    window = _time_window_from_query(query, now or datetime.now().astimezone())

    def boosted(c: RagChunk) -> float:
        return (
            _adjusted_score(c, conv_query=conv_query)
            + _grep_boost(c, norm_query, keywords)
            + _source_nudge(c, conv_query=conv_query)
            + _time_boost(c, window)
        )

    capped.sort(key=boosted, reverse=True)
    return capped[:_PROMPT_RENDER_TOP_N]


def _reorder_for_long_context(
    chunks: list[RagChunk], *, head: int = 20, tail: int = 20
) -> list[RagChunk]:
    """Lost-in-the-middle 对策(D-new, rag_redesign_2026-05-28)。

    long-context LLM 在 prompt 中段 attention 显著衰减。本函数把:
    - 最相关 top-`head` chunks(默认 1-20)放到 prompt 头部
    - 次相关 chunks[head:head+tail](默认 21-40)放到 prompt 尾部
    - 剩余 chunks[head+tail:](默认 41-80)塞到 prompt 中段
    使"被注意到"的位置承载"被认为最重要"的 chunk。

    输入预期已经按 (source 优先级, score+boost) 排好序。
    """
    head_chunks = chunks[:head]
    tail_chunks = chunks[head : head + tail]
    middle_chunks = chunks[head + tail :]
    return head_chunks + middle_chunks + tail_chunks


# === 渲染 ===


def _format_rag(chunks: list[RagChunk]) -> str:
    """渲染 RAG 块:输入预期已是 top-80(rerank 之后),走 lost-in-middle 重排。"""
    if not chunks:
        return "(无)"
    reordered = _reorder_for_long_context(chunks, head=20, tail=20)
    lines: list[str] = []
    for c in reordered:
        page = c.metadata.get("page")
        head = f"[doc:{c.chunk_id}"
        if page:
            head += f" p{page}"
        head += f" {c.doc_title}]"
        lines.append(f"{head}\n{c.text}")
    return "\n\n".join(lines)


def _format_web(hits: list[WebHit]) -> str:
    if not hits:
        return "(无)"
    return "\n\n".join(f"[web:{h.url}] {h.title}\n{h.snippet}" for h in hits[:5])


def _build_rag_guidance(n_chunks: int) -> str:
    """生成 RAG 块前的引导文案:告诉 main LLM 一共有几段、按什么排序、要怎么读。

    无 RAG 命中时返回空串,避免凭空多一行噪音。
    """
    if n_chunks <= 0:
        return ""
    return (
        f"下方共 {n_chunks} 段证据(按相关度大致排序,注意中段也可能含答案);"
        "请综合通读后再答;如证据相互矛盾请标注;引用时给出 chunk 编号。\n\n"
    )


# === 分类 ===


async def _classify(fast_llm: LLMPort, fast_model: str, question: str) -> str:
    # P2.3：fast LLM 失败时不让整条 RAG/web 链路 raise；退到 "either"
    # 让两条检索路径都跑，最终交给 main_llm 综合。fast LLM 是 heyi-bj
    # 7860，远端断时这里 timeout / connection error 都算降级。
    try:
        resp = await fast_llm.chat(
            [
                ChatMessage(role="system", content=_CLASSIFIER_PROMPT),
                ChatMessage(role="user", content=question),
            ],
            model=fast_model,
            max_tokens=20,
            temperature=0.0,
            timeout_s=30.0,
        )
    except Exception as e:
        import logging

        logging.getLogger("echodesk.retrieve").warning(
            "intent classifier (fast LLM) failed, fallback to 'either': %s", e
        )
        return "either"
    out = (resp.content or "").strip().lower()
    # 兼容 LLM 输出标点/解释
    for label in ("rag", "web", "either"):
        if label in out:
            return label
    return "either"


# === 主入口 ===


async def retrieve_and_answer(
    *,
    main_llm: LLMPort,
    fast_llm: LLMPort,
    fast_model: str,
    rag: RagPort,
    web: WebSearchPort,
    question: str,
    rag_top_k: int = _DEFAULT_RAG_TOP_K,
    web_top_n: int = 5,
    inline_context: str | None = None,
) -> AnswerStream:
    """先分类→粗召回 top_k=1000→doc-cap+grep boost+source 排序 top-80→
    lost-in-middle 重排→拼 prompt→流式答复。

    ``inline_context``：前端传入的"当前会议 / 最近 ambient 转录"作为额外上下文
    块直接拼到 prompt（不进 RAG 索引）。用户 2026-05-28 反馈："Echo 的回复
    显然没有带上下文" → 提供这个通道让 Echo 实时感知正在进行的会议。
    """
    cls = await _classify(fast_llm, fast_model, question)

    rag_chunks: list[RagChunk] = []
    web_hits: list[WebHit] = []
    try:
        if cls in {"rag", "either"}:
            rag_chunks = await rag.query(question, top_k=rag_top_k)
            # doc-cap=12 + grep-style 字面提升 + source 软排序 → top-80
            # (D-new, rag_redesign_2026-05-28)
            rag_chunks = _rerank_diverse_with_priority_and_grep_boost(rag_chunks, question)
    except Exception:
        rag_chunks = []
    try:
        if cls in {"web", "either"}:
            web_hits = await web.search(question, top_n=web_top_n)
    except Exception:
        web_hits = []

    chosen = (
        "rag" if rag_chunks and not web_hits else "web" if web_hits and not rag_chunks else "both"
    )
    retrieval = RetrievalResult(
        query=question,
        rag_chunks=rag_chunks,
        web_hits=web_hits,
        arbitration={"classifier": 1.0 if cls in {"rag", "web"} else 0.5},
        chosen_source=chosen,
    )

    prompt = _ANSWER_PROMPT_TEMPLATE.format(
        inline_context_block=(
            inline_context.strip() if inline_context and inline_context.strip() else "(无)"
        ),
        rag_guidance=_build_rag_guidance(len(rag_chunks)),
        rag_block=_format_rag(rag_chunks),
        web_block=_format_web(web_hits),
        question=question,
    )

    async def _gen() -> AsyncIterator[str]:
        async for chunk in main_llm.chat_stream(
            [ChatMessage(role="user", content=prompt)],
        ):
            yield chunk

    return AnswerStream(retrieval=retrieval, chunks=_gen())
