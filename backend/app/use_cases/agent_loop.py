"""Agent loop: 让主 LLM 自己串联 rag_search / web_search / generate_artifact / final_answer。

为什么这样做（2026-05-28 用户反馈）:
- 旧链路: intent router 一次只能挑一个工具。"做 heyi 竞品调研并输出 HTML" 被
  路由成 generate_html, brief 直送 skill, skill prompt 不看 RAG/Web → LLM 编。
- 新链路: 主 LLM 在一个对话循环里自己决定调哪个工具,可以 rag_search →
  web_search → generate_artifact → final_answer 串起来。

协议(文本式 tool call, 不依赖 OpenAI function calling, 跨模型可用):
- 每一步 LLM 只输出一个 JSON 对象, 严格符合下列两种格式之一:
  A) {"action":"tool_call","tool":"<name>","args":{...},"reason":"<≤30 字>"}
  B) {"action":"final","answer":"<最终 markdown>"}
- 工具执行结果以 user 消息追加, 内容是 ``ToolResult.content`` 字段。

为什么不用原生 function calling: MiniMax/Qwen/GLM 在 yunwu 代理后行为不一致,
JSON 解析路线已经在 ``llm_router._extract_from_raw`` 里被证明能跑稳。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from app.adapters.llm import LLMError
from app.adapters.skill import SkillError
from app.config import Settings
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.skill import SkillExecutorPort
from app.ports.web_search import WebSearchPort
from app.schemas.agent import AgentEvent, ToolResult
from app.schemas.artifact import SUPPORTED_KINDS, GeneratedArtifact
from app.schemas.llm import ChatMessage
from app.use_cases.local_datetime import answer_local_datetime
from app.use_cases.retrieve_and_answer import _rerank_diverse_with_priority_and_grep_boost
from app.use_cases.style_template import merge_extra_instructions, resolve_docx_style_template

_log = logging.getLogger("echodesk.agent")

_DEFAULT_MAX_ITERATIONS: Final[int] = 6
_LLM_TIMEOUT_S: Final[float] = 120.0
# 单步 LLM 调用的瞬时失败重试（云端偶发断流/超时），避免一次抖动就崩掉整轮对话。
_LLM_STEP_RETRIES: Final[int] = 1
_LLM_STEP_RETRY_SLEEP_S: Final[float] = 1.5
# 单次 chat 的 max_tokens. 4k 够长 JSON tool_call + 中等 final_answer;
# generate_artifact 才是大输出, 由 skill 内部自己跑 12k token.
_AGENT_STEP_MAX_TOKENS: Final[int] = 4000
_RAG_CHUNK_EXCERPT_CHARS: Final[int] = 800
_WEB_SNIPPET_CHARS: Final[int] = 500
_CITATION_TEXT_CHARS: Final[int] = 1200
_AGENT_TEMPERATURE: Final[float] = 0.2
# delta 切片大小: 让前端有"流式打字"观感, 不阻塞太多。
_DELTA_CHUNK_CHARS: Final[int] = 80
# 避免 LLM 误调 final 时把空答案塞过来。
_MIN_FINAL_ANSWER_CHARS: Final[int] = 1
_GROUNDING_TERMS: Final[tuple[str, ...]] = (
    "heyi",
    "heyi100",
    "hy100",
    "hy90",
    "褐蚁",
    "型号",
    "配置",
    "价格",
    "手册",
    "竞品",
    "市场",
    "调研",
    "生态位",
    "对比",
)
_WEB_GROUNDING_TERMS: Final[tuple[str, ...]] = (
    "最新",
    "新闻",
    "市场",
    "竞品",
    "调研",
    "生态位",
    "价格",
    "行情",
)
_FACTUAL_ARTIFACT_TERMS: Final[tuple[str, ...]] = (
    "调研",
    "研究",
    "分析",
    "竞品",
    "市场",
    "招投标",
    "投标",
    "现状",
    "应用",
    "价格",
    "参数",
)
_ARTIFACT_TYPE_HINTS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("pptx", ("ppt", "pptx", "幻灯片", "演示文稿", "deck", "presentation")),
    ("xlsx", ("excel", "xlsx", "表格", "电子表格", "工作簿", "spreadsheet")),
    ("word", ("word", "docx", "文档", "报告书", "方案书")),
    ("html", ("html", "网页", "页面", "one-pager", "onepager")),
    ("markdown", ("markdown", "md")),
    ("pdf", ("pdf",)),
    ("txt", ("txt", "文本")),
)
_ARTIFACT_FACT_GUARDRAIL: Final[str] = (
    "只能使用 brief 里给出的事实。没有数据的指标写'资料不足', 不要编造 MAU/ARR/价格/"
    "份额。每个数字给出来源(doc 标题或 URL)。"
)

_SYS_PROMPT_TEMPLATE: Final[str] = """你是 EchoDesk 桌面助手 Echo, 可以串联调用以下工具完成复合任务。

# 当前时间
{current_datetime}

# 可用工具

- rag_search(query: string, top_k: int = 20)
  在本地知识库(PDF/会议纪要/ambient 转录/上传文件)检索证据。返回前 K 个 chunk 的标题+页码+摘要。
- web_search(query: string, top_n: int = 5)
  Tavily(主)+DDG(兜底)联网。返回标题+摘要+URL。
- generate_artifact(artifact_type: string, brief: string, extra_instructions: string?)
  生成产物文件。artifact_type ∈ {{html, pptx, word, xlsx, markdown, pdf, txt}}。
- final_answer(answer: string)
  把最终答复(markdown)发给用户, 结束本轮。

# 工作原则

1. 先想清楚用户的真实目标。复合任务(如"调研 X 并输出 HTML")拆成多步: 先 search 收集证据, 再 generate, 最后 final_answer 总结。
2. 凡涉及具体事实(名称/价格/数字/对比/产品配置), 必须先 rag_search 至少一次; 若用户原话提到"最新""今年""新闻"等时效信号, 再补 web_search。无证据不要编造。
3. 调 generate_artifact 时:
   - ``brief`` 必须把检索到的关键事实抄进去(不要让 skill 重新想)。
   - ``extra_instructions`` 必须包含: "只能使用 brief 里给出的事实。没有数据的指标写'资料不足', 不要编造 MAU/ARR/价格/份额。每个数字给出来源(doc 标题或 URL)。"
4. 不超过 6 次工具调用。任何工具失败都不要重试超过 1 次, 改用 final_answer 把已知结果交给用户。
5. 调过 generate_artifact 后, final_answer 简短提及"已生成 <kind> 产物, 标题 <title>"即可, 不要重复 dump 产物内容。
6. 没有需要调工具的简单寒暄(含日期/时间/问候等) → 直接 final_answer, 不调任何工具。

# 输出协议(每一步必须严格遵守)

只输出**一个**JSON 对象, 没有前后缀文字, 没有 markdown 围栏。两种格式择一:

{{"action":"tool_call","tool":"<name>","args":{{...}},"reason":"<一句话决策原因>"}}

{{"action":"final","answer":"<最终 markdown 回答>"}}

输出格式错误会被系统拒绝并要求重发。
"""

_ACTION_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


@dataclass(slots=True)
class _ToolDeps:
    """工具执行需要的端口集合 + 共享 settings。"""

    rag: RagPort
    web: WebSearchPort
    skill: SkillExecutorPort
    main_llm: LLMPort
    settings: Settings


_ToolHandler = Callable[[dict[str, Any], _ToolDeps], Awaitable[ToolResult]]


async def _tool_rag_search(args: dict[str, Any], deps: _ToolDeps) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(
            name="rag_search",
            ok=False,
            content="rag_search 参数错误: query 为空",
            summary="参数错误",
        )
    try:
        top_k = int(args.get("top_k", 20))
    except (TypeError, ValueError):
        top_k = 20
    top_k = max(1, min(top_k, 60))
    try:
        chunks = await deps.rag.query(query, top_k=top_k)
        chunks = _rerank_diverse_with_priority_and_grep_boost(chunks, query)
    except Exception as e:
        _log.warning("agent rag_search failed: %s", e)
        return ToolResult(
            name="rag_search",
            ok=False,
            content=f"rag_search 失败: {e}",
            summary="检索异常",
        )
    if not chunks:
        return ToolResult(
            name="rag_search",
            ok=True,
            content="(本地知识库未命中相关 chunk)",
            summary="0 命中",
        )
    lines: list[str] = [f"共检索到 {len(chunks)} chunk, 列出前 {min(len(chunks), 12)}:"]
    for i, c in enumerate(chunks[:12], 1):
        page = c.metadata.get("page")
        page_label = f" p{page}" if page else ""
        kind = c.metadata.get("kind") or c.metadata.get("source") or "doc"
        excerpt = c.text.replace("\n", " ").strip()
        if len(excerpt) > _RAG_CHUNK_EXCERPT_CHARS:
            excerpt = excerpt[: _RAG_CHUNK_EXCERPT_CHARS - 1].rstrip() + "…"
        lines.append(f"[R{i}] {c.doc_title}{page_label} · {kind} · score={c.score:.2f}\n{excerpt}")
    citations = [
        {
            "kind": "rag",
            "doc_id": c.doc_id,
            "chunk_id": c.chunk_id,
            "doc_title": c.doc_title,
            "title": c.doc_title,
            "page": c.metadata.get("page"),
            "source": c.metadata.get("source") or c.metadata.get("kind") or "rag",
            "score": c.score,
            "text": _excerpt(c.text, _CITATION_TEXT_CHARS),
            "snippet": _excerpt(c.text, 360),
        }
        for c in chunks[:12]
    ]
    return ToolResult(
        name="rag_search",
        ok=True,
        content="\n\n".join(lines),
        summary=f"检索 {len(chunks)} chunk",
        metadata={"n_chunks": len(chunks), "query": query, "citations": citations},
    )


async def _tool_web_search(args: dict[str, Any], deps: _ToolDeps) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(
            name="web_search",
            ok=False,
            content="web_search 参数错误: query 为空",
            summary="参数错误",
        )
    try:
        top_n = int(args.get("top_n", 5))
    except (TypeError, ValueError):
        top_n = 5
    top_n = max(1, min(top_n, 10))
    try:
        hits = await deps.web.search(query, top_n=top_n)
    except Exception as e:
        _log.warning("agent web_search failed: %s", e)
        return ToolResult(
            name="web_search",
            ok=False,
            content=f"web_search 失败: {e}",
            summary="联网失败",
        )
    if not hits:
        return ToolResult(
            name="web_search",
            ok=True,
            content="(web 未返回结果, 可能是关键词太具体或网络受限)",
            summary="0 命中",
        )
    lines: list[str] = [f"共 {len(hits)} 条 web 结果:"]
    for i, h in enumerate(hits, 1):
        snippet = h.snippet.replace("\n", " ").strip()
        if len(snippet) > _WEB_SNIPPET_CHARS:
            snippet = snippet[: _WEB_SNIPPET_CHARS - 1].rstrip() + "…"
        lines.append(f"[W{i}] {h.title} · {h.source}\nURL: {h.url}\n{snippet}")
    citations = [
        {
            "kind": "web",
            "url": h.url,
            "title": h.title,
            "source": h.source,
            "score": h.score,
            "snippet": _excerpt(h.snippet, _WEB_SNIPPET_CHARS),
        }
        for h in hits
    ]
    return ToolResult(
        name="web_search",
        ok=True,
        content="\n\n".join(lines),
        summary=f"联网 {len(hits)} 条",
        metadata={"n_hits": len(hits), "query": query, "citations": citations},
    )


async def _tool_generate_artifact(args: dict[str, Any], deps: _ToolDeps) -> ToolResult:
    artifact_type = str(args.get("artifact_type", "")).strip().lower()
    brief = str(args.get("brief", "")).strip()
    extra_instructions = args.get("extra_instructions")
    if isinstance(extra_instructions, str):
        extra_instructions = extra_instructions.strip() or None
    else:
        extra_instructions = None
    if artifact_type not in SUPPORTED_KINDS:
        return ToolResult(
            name="generate_artifact",
            ok=False,
            content=(
                f"generate_artifact 参数错误: artifact_type='{artifact_type}' 不支持。"
                f"可选: {sorted(SUPPORTED_KINDS)}"
            ),
            summary="参数错误",
        )
    if not brief:
        return ToolResult(
            name="generate_artifact",
            ok=False,
            content="generate_artifact 参数错误: brief 为空",
            summary="参数错误",
        )
    # 自动发现知识库里的同类 .docx 当样式模板，抽离版式注入（仅 Word）。
    eff_extra = extra_instructions
    template_note = ""
    if artifact_type.lower() in {"word", "docx"}:
        tpl = await resolve_docx_style_template(deps.rag, brief)
        if tpl is not None:
            eff_extra = merge_extra_instructions(extra_instructions, tpl.instructions)
            template_note = f", 参考知识库模板: {tpl.title}"
    try:
        artifact: GeneratedArtifact = await deps.skill.generate(
            llm=deps.main_llm,
            artifact_type=artifact_type,
            brief=brief,
            extra_instructions=eff_extra,
        )
    except (SkillError, LLMError) as e:
        _log.warning("agent generate_artifact failed: %s", e)
        return ToolResult(
            name="generate_artifact",
            ok=False,
            content=f"generate_artifact 失败: {e}",
            summary=f"生成 {artifact_type} 失败",
        )
    summary = (
        f"已生成 {artifact.artifact_type} 产物 "
        f"(id={artifact.artifact_id}, {artifact.size_bytes / 1024:.1f} KB)"
    )
    return ToolResult(
        name="generate_artifact",
        ok=True,
        content=(
            f"{summary}, 标题: {artifact.title or '(无)'}{template_note}, "
            f"latency_ms={artifact.generation_latency_ms:.0f}, "
            f"path={artifact.file_path}"
        ),
        summary=summary,
        metadata={"artifact": artifact.model_dump(mode="json")},
    )


_TOOLS: Final[dict[str, _ToolHandler]] = {
    "rag_search": _tool_rag_search,
    "web_search": _tool_web_search,
    "generate_artifact": _tool_generate_artifact,
}


# 纯寒暄/客套的封闭集合：只有这些"明显不需要知识库"的问题才跳过检索。
# 设计原则（2026-06-04 用户事故复盘）：跨对话/历史检索不能靠关键词正则去"猜"
# 是不是历史查询——命中率不可控。改为"默认带知识库"（与 UI 文案一致）：除下面
# 这个封闭集合外，任何实质问题都先 rag_search，命中交给 BM25 + 相关度排序。
# 把"跳过"判错也无害：顶多对一句寒暄多跑一次本地 BM25。
_TRIVIAL_CHITCHAT: Final[frozenset[str]] = frozenset(
    {
        "你好",
        "您好",
        "哈喽",
        "哈罗",
        "嗨",
        "hi",
        "hello",
        "hey",
        "在吗",
        "在不在",
        "在",
        "你在吗",
        "谢谢",
        "多谢",
        "感谢",
        "谢啦",
        "再见",
        "拜拜",
        "晚安",
        "早",
        "早上好",
        "晚上好",
        "ok",
        "okay",
        "好的",
        "好",
        "嗯",
        "收到",
    }
)
_TRIVIAL_STRIP_RE = re.compile(r"[\s,，。.!！?？~、:：;；@]+")


def _is_trivial_chitchat(question: str) -> bool:
    """判断是否为纯寒暄（去标点后落在封闭集合）。仅用于"跳过检索"，判错无害。"""
    q = _TRIVIAL_STRIP_RE.sub("", question.strip()).lower()
    return q in _TRIVIAL_CHITCHAT


def _needs_initial_web(question: str) -> bool:
    lower = question.lower()
    return any(term in lower for term in _WEB_GROUNDING_TERMS) or (
        _requested_artifact_type(question) is not None
        and any(term in lower for term in _FACTUAL_ARTIFACT_TERMS)
    )


def _rag_grounding_query(question: str) -> str:
    lower = question.lower()
    if any(term in lower for term in ("heyi", "heyi100", "hy100", "hy90", "褐蚁")):
        return f"褐蚁 HY100 HY90 heyi heyi100 产品手册 型号 配置 竞品 生态位 {question}"
    return question


def _web_grounding_query(question: str) -> str:
    lower = question.lower()
    if any(term in lower for term in ("heyi", "heyi100", "hy100", "hy90", "褐蚁")):
        return f"褐蚁 HY100 本地大模型 算力一体机 竞品 市场 {question}"
    return question


def _append_unique_citations(
    citations: list[dict[str, Any]],
    raw_citations: object,
) -> None:
    if not isinstance(raw_citations, list):
        return
    for citation in raw_citations:
        if not isinstance(citation, dict):
            continue
        key = citation.get("chunk_id") or citation.get("url") or citation.get("doc_id")
        if key and not any(
            (c.get("chunk_id") or c.get("url") or c.get("doc_id")) == key for c in citations
        ):
            citations.append(citation)


def _strip_fence(raw: str) -> str:
    """解析时容忍 ```json ... ``` 围栏。"""
    raw = raw.strip()
    if raw.startswith("```"):
        m = _ACTION_FENCE_RE.search(raw)
        if m:
            return m.group(1).strip()
    return raw


def _decode_first_json_object(text: str) -> dict[str, Any] | None:
    """从文本中抽取第一个合法 JSON object。

    比 ``text[first_brace:last_brace]`` 更稳：模型有时会连续输出两个 JSON
    对象，或在后面追加解释；``raw_decode`` 能在第一个对象结束处停止。
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_action(raw: str) -> dict[str, Any] | None:
    """解析 LLM 输出。返回 dict 或 None(让 caller 提示重发)。"""
    text = _strip_fence(raw)
    parsed = _decode_first_json_object(text)
    if parsed is None:
        return None
    action = parsed.get("action")
    if action == "final_answer":
        parsed["action"] = "final"
        if "answer" not in parsed and isinstance(parsed.get("args"), dict):
            parsed["answer"] = parsed["args"].get("answer", "")
        return parsed
    if action not in {"tool_call", "final"}:
        return None
    if action == "tool_call" and str(parsed.get("tool", "")).strip() == "final_answer":
        args = parsed.get("args")
        parsed = {"action": "final", "answer": ""}
        if isinstance(args, dict):
            parsed["answer"] = str(args.get("answer", ""))
        return parsed
    return parsed


def _requested_artifact_type(question: str) -> str | None:
    lower = question.lower()
    for artifact_type, hints in _ARTIFACT_TYPE_HINTS:
        if any(hint in lower for hint in hints):
            return artifact_type
    return None


def _fallback_brief(question: str, evidence_parts: list[str]) -> str:
    sections = [f"用户目标:\n{question.strip()}"]
    if evidence_parts:
        sections.append("已检索到的证据摘要:\n" + "\n\n".join(evidence_parts[-8:]))
    else:
        sections.append("可用证据不足；产物中缺失的信息必须标注为'资料不足'。")
    return "\n\n".join(sections)[:12_000]


async def _chunked_deltas(answer: str) -> AsyncIterator[str]:
    """切 final answer 成 ~80 字 chunk, 让前端有打字效果。"""
    n = len(answer)
    if n == 0:
        return
    i = 0
    while i < n:
        j = min(i + _DELTA_CHUNK_CHARS, n)
        yield answer[i:j]
        i = j


def _excerpt(text: str, max_chars: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


async def _agent_step_chat(main_llm: LLMPort, messages: list[ChatMessage]) -> str:
    """跑一步主 LLM 调用，带瞬时失败重试（云端偶发抖动不应崩掉整轮对话）。

    成功返回 raw 文本；全部重试用尽抛最后一个 ``LLMError`` 给上层处理。
    """
    last_err: LLMError | None = None
    for attempt in range(_LLM_STEP_RETRIES + 1):
        try:
            resp = await main_llm.chat(
                messages,
                max_tokens=_AGENT_STEP_MAX_TOKENS,
                temperature=_AGENT_TEMPERATURE,
                timeout_s=_LLM_TIMEOUT_S,
            )
            return (resp.content or "").strip()
        except LLMError as e:
            last_err = e
            if attempt < _LLM_STEP_RETRIES:
                _log.warning("agent step LLM 调用失败，%.1fs 后重试: %s", _LLM_STEP_RETRY_SLEEP_S, e)
                await asyncio.sleep(_LLM_STEP_RETRY_SLEEP_S)
    raise last_err if last_err is not None else LLMError("agent step LLM 调用失败")


async def _direct_chat_answer(
    main_llm: LLMPort, question: str, evidence_parts: list[str]
) -> str:
    """绕过工具编排协议，直接让 LLM 用普通中文 markdown 回答问题。

    用途：当模型反复无法产出合法的 tool_call / final JSON（编排协议太难），但用户
    问的其实是个可直接回答的问题（如"总结 X 市场现状"）时，退回最朴素、最稳的
    一次性问答——不要求任何 JSON 协议，几乎不会再失败。带上已检索到的证据。
    """
    try:
        resp = await main_llm.chat(
            _direct_answer_messages(question, evidence_parts, None),
            max_tokens=2000,
            temperature=0.4,
            timeout_s=_LLM_TIMEOUT_S,
        )
        return (resp.content or "").strip()
    except LLMError as e:
        _log.warning("direct chat answer failed: %s", e)
        return ""


def _direct_answer_messages(
    question: str, evidence_parts: list[str], inline_context: str | None
) -> list[ChatMessage]:
    """组装"纯直答"的 messages（流式/非流式共用，保证两条路答案口径一致）。"""
    now = datetime.now(timezone.utc).astimezone()  # noqa: UP017 - 保留 timezone.utc（曾因 datetime.UTC 触发 AttributeError）
    current_dt_str = now.strftime("%Y年%m月%d日 %H:%M %Z（%A）")
    sys = (
        "你是 EchoDesk 桌面助手 Echo。直接用简洁、有条理的中文 markdown 回答用户问题。"
        "若提供了参考资料就结合资料作答，并在末尾标注来源；没有就用你已有的知识回答。"
        f"当前本地时间是：{current_dt_str}。"
        "不要输出 JSON、不要任何工具协议、不要解释你的思考过程。"
    )
    user = question.strip()
    if inline_context and inline_context.strip():
        user += "\n\n# 当前上下文(最近转录, 仅供参考)\n" + inline_context.strip()[:2000]
    if evidence_parts:
        user += "\n\n# 参考资料\n" + "\n\n".join(evidence_parts[-4:])
    return [
        ChatMessage(role="system", content=sys),
        ChatMessage(role="user", content=user),
    ]


async def _direct_chat_answer_stream(
    main_llm: LLMPort,
    question: str,
    evidence_parts: list[str],
    inline_context: str | None = None,
) -> AsyncIterator[str]:
    """流式版直答：首字延迟优化的核心——token 一到就吐给前端，不等整段生成完。"""
    async for chunk in main_llm.chat_stream(
        _direct_answer_messages(question, evidence_parts, inline_context),
        max_tokens=2000,
        temperature=0.4,
        timeout_s=_LLM_TIMEOUT_S,
    ):
        if chunk:
            yield chunk


async def _forced_final_answer(
    main_llm: LLMPort,
    messages: list[ChatMessage],
    evidence_parts: list[str],
    question: str,
) -> str:
    """步数用尽时，保证给用户一个真实回答，绝不只抛错。

    三级兜底：①让模型按 final JSON 收尾；②纯 LLM 直答（无协议，最稳）；
    ③已检索证据摘要。任一成功即返回。
    """
    nudge = ChatMessage(
        role="user",
        content=(
            "已达到工具调用上限，现在**必须**只输出一个 "
            '{"action":"final","answer":"…"} JSON：基于以上已获得的信息直接给出'
            "尽可能有用的最终回答；信息不足处如实说明，不要再调用任何工具。"
        ),
    )
    try:
        raw = await _agent_step_chat(main_llm, [*messages, nudge])
        parsed = _parse_action(raw)
        if parsed and parsed.get("action") == "final":
            answer = str(parsed.get("answer", "")).strip()
            if answer:
                return answer
    except LLMError as e:
        _log.warning("forced final answer 调用失败: %s", e)
    # ② 纯 LLM 直答（绕过协议）
    direct = await _direct_chat_answer(main_llm, question, evidence_parts)
    if direct:
        return direct
    # ③ 证据摘要兜底
    if evidence_parts:
        return (
            "我已尽力检索，以下是已获得的关键信息摘要，供你参考：\n\n"
            + "\n\n".join(evidence_parts[-3:])
        )
    return "抱歉，这个问题这次没能处理完。请把问题拆细一些或换个问法，我再试一次。"


async def run_agent(  # noqa: PLR0911, PLR0912, PLR0915 - agent loop is intentionally linear for auditability
    *,
    main_llm: LLMPort,
    rag: RagPort,
    web: WebSearchPort,
    skill: SkillExecutorPort,
    settings: Settings,
    question: str,
    inline_context: str | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    enable_fast_path: bool = False,
    auto_retrieve: bool = True,
) -> AsyncIterator[AgentEvent]:
    """跑一轮 agent 循环, 输出 AgentEvent stream。

    遇到 LLMError 直接 yield error+done 退出; 工具失败包成 ToolResult 喂回模型,
    让 LLM 自己决定是否再调一次或直接 final_answer。
    """
    if not question.strip():
        yield AgentEvent(type="error", payload={"error": "question 为空", "stage": "input"})
        yield AgentEvent(type="done")
        return

    local_answer = answer_local_datetime(question)
    if local_answer is not None:
        async for delta in _chunked_deltas(local_answer):
            yield AgentEvent(type="delta", payload={"text": delta})
        yield AgentEvent(
            type="final",
            payload={"answer": local_answer, "artifact_ids": [], "citations": []},
        )
        yield AgentEvent(type="done")
        return

    deps = _ToolDeps(rag=rag, web=web, skill=skill, main_llm=main_llm, settings=settings)
    now = datetime.now(timezone.utc).astimezone()  # noqa: UP017 - 保留 timezone.utc（曾因 datetime.UTC 触发 AttributeError）
    current_dt_str = now.strftime("%Y年%m月%d日 %H:%M %Z（%A）")
    sys_prompt = _SYS_PROMPT_TEMPLATE.format(current_datetime=current_dt_str)
    messages: list[ChatMessage] = [ChatMessage(role="system", content=sys_prompt)]
    if inline_context and inline_context.strip():
        messages.append(
            ChatMessage(
                role="system",
                content="当前会议上下文(最近转录, 仅供参考):\n" + inline_context.strip(),
            )
        )
    messages.append(ChatMessage(role="user", content=question.strip()))

    artifact_ids: list[str] = []
    citations: list[dict[str, Any]] = []
    evidence_parts: list[str] = []
    format_retries = 0
    max_format_retries = 2
    seen_calls: set[str] = set()
    # 用户明确要产物时,记录该类型;若模型想空手 final,先纠偏一次让它真正生成产物
    # （复合任务"调研X并输出PPT"经常 rag 完就文字收尾,不闭环）。
    required_artifact = _requested_artifact_type(question)
    artifact_nudged = False
    artifact_attempted = False  # 是否已尝试过 generate_artifact（成功或失败都算）

    prelude_calls: list[tuple[str, dict[str, Any], str]] = []
    # 默认带知识库：除纯寒暄外，任何实质问题都先检索本地知识库/历史对话。
    # 命中靠 BM25 + 相关度排序，而不是用关键词正则去猜"是不是历史查询"
    # （修 2026-06-04 跨对话历史检索失效：用户问"前几天河南的需求对接谁负责"
    # 因不含 grounding 关键词被当寒暄直答，从不检索历史）。
    if auto_retrieve and not _is_trivial_chitchat(question):
        prelude_calls.append(
            (
                "rag_search",
                {"query": _rag_grounding_query(question), "top_k": 40},
                "默认检索本地知识库/历史对话做事实锚定",
            )
        )
    if _needs_initial_web(question):
        prelude_calls.append(
            (
                "web_search",
                {"query": _web_grounding_query(question), "top_n": 5},
                "补充外部市场/竞品信息",
            )
        )

    # 首字延迟优化：无需任何工具锚定的简单问答(不查库/不联网/不产物) → 直接走流式直答，
    # 省掉"非流式 JSON 决策 + 整段回放"的首字延迟，token 一到就吐给前端。
    if enable_fast_path and not prelude_calls and required_artifact is None:
        yield AgentEvent(type="plan", payload={"step": 1, "max_steps": max_iterations})
        acc: list[str] = []
        try:
            async for chunk in _direct_chat_answer_stream(
                main_llm, question, evidence_parts, inline_context
            ):
                acc.append(chunk)
                yield AgentEvent(type="delta", payload={"text": chunk})
        except LLMError as e:
            _log.warning("fast-path 流式直答失败，回退完整 agent 循环: %s", e)
        answer = "".join(acc).strip()
        if answer:
            yield AgentEvent(
                type="final",
                payload={"answer": answer, "artifact_ids": [], "citations": []},
            )
            yield AgentEvent(type="done")
            return
        # 流式一个字都没出来 → 不在此收尾，落到下面完整 agent 循环兜底（更稳）

    for tool_name, args, reason in prelude_calls:
        yield AgentEvent(
            type="tool_call",
            payload={"name": tool_name, "args": args, "reason": reason, "step": 0},
        )
        prelude_handler = _TOOLS[tool_name]
        result = await prelude_handler(args, deps)
        _append_unique_citations(citations, result.metadata.get("citations"))
        yield AgentEvent(
            type="tool_result",
            payload={
                "name": result.name,
                "ok": result.ok,
                "summary": result.summary,
                "step": 0,
            },
        )
        messages.append(
            ChatMessage(
                role="user",
                content=f"预置工具 {result.name} 结果 (ok={result.ok}):\n{result.content}",
            )
        )
        if result.ok and result.name in {"rag_search", "web_search"}:
            evidence_parts.append(f"[{result.name}]\n{result.content}")

    for step in range(1, max_iterations + 1):
        yield AgentEvent(type="plan", payload={"step": step, "max_steps": max_iterations})

        try:
            raw = await _agent_step_chat(main_llm, messages)
        except LLMError as e:
            # 编排调用失败（含 M2.7 偶发敏感词 500）→ 先试一次"纯 LLM 直答"
            # （更短更朴素的 prompt 往往能绕过），仍失败才把错误交给用户。
            direct = await _direct_chat_answer(main_llm, question, evidence_parts)
            if direct:
                async for delta in _chunked_deltas(direct):
                    yield AgentEvent(type="delta", payload={"text": delta})
                yield AgentEvent(
                    type="final",
                    payload={
                        "answer": direct,
                        "artifact_ids": list(artifact_ids),
                        "citations": list(citations),
                    },
                )
                yield AgentEvent(type="done")
                return
            yield AgentEvent(type="error", payload={"error": str(e), "stage": "llm"})
            yield AgentEvent(type="done")
            return

        parsed = _parse_action(raw)
        if parsed is None:
            format_retries += 1
            if format_retries > max_format_retries:
                fallback_kind = _requested_artifact_type(question)
                if fallback_kind is not None:
                    args = {
                        "artifact_type": fallback_kind,
                        "brief": _fallback_brief(question, evidence_parts),
                        "extra_instructions": _ARTIFACT_FACT_GUARDRAIL,
                    }
                    reason = "模型编排输出格式异常，按用户明确产物要求直接生成"
                    yield AgentEvent(
                        type="tool_call",
                        payload={
                            "name": "generate_artifact",
                            "args": args,
                            "reason": reason,
                            "step": step,
                        },
                    )
                    result = await _tool_generate_artifact(args, deps)
                    art = result.metadata.get("artifact")
                    if isinstance(art, dict) and art.get("artifact_id"):
                        artifact_ids.append(str(art["artifact_id"]))
                        yield AgentEvent(type="artifact", payload=art)
                    yield AgentEvent(
                        type="tool_result",
                        payload={
                            "name": result.name,
                            "ok": result.ok,
                            "summary": result.summary,
                            "step": step,
                        },
                    )
                    if result.ok and isinstance(art, dict):
                        title = str(art.get("title") or art.get("artifact_id") or fallback_kind)
                        answer = (
                            f"模型编排输出格式不稳定，我已按你的明确要求直接生成 "
                            f"{fallback_kind} 产物：{title}。"
                        )
                        async for delta in _chunked_deltas(answer):
                            yield AgentEvent(type="delta", payload={"text": delta})
                        yield AgentEvent(
                            type="final",
                            payload={
                                "answer": answer,
                                "artifact_ids": list(artifact_ids),
                                "citations": list(citations),
                            },
                        )
                    else:
                        yield AgentEvent(
                            type="error",
                            payload={
                                "error": result.content or "产物生成失败",
                                "stage": "generate_artifact",
                            },
                        )
                    yield AgentEvent(type="done")
                    return
                # 非产物请求 + 编排协议反复失败 → 退回最稳的"纯 LLM 直答"，
                # 绝不把"agent 格式错误"这种内部异常甩给用户（用户问的多半是
                # 可直接回答的问题，如"总结 X 市场现状"）。
                answer = await _direct_chat_answer(main_llm, question, evidence_parts)
                if not answer:
                    answer = "我已尽力检索，但这次没能完整组织答案。请再说一次或换个问法。"
                async for delta in _chunked_deltas(answer):
                    yield AgentEvent(type="delta", payload={"text": delta})
                yield AgentEvent(
                    type="final",
                    payload={
                        "answer": answer,
                        "artifact_ids": list(artifact_ids),
                        "citations": list(citations),
                    },
                )
                yield AgentEvent(type="done")
                return
            messages.append(ChatMessage(role="assistant", content=raw))
            messages.append(
                ChatMessage(
                    role="user",
                    content=(
                        "输出格式错误, 必须只输出一个 JSON 对象, "
                        "字段 action 必须是 'tool_call' 或 'final'。请重发。"
                    ),
                )
            )
            continue

        action = parsed["action"]
        if action == "final":
            # 复合任务闭环守卫:用户明确要产物,但模型从没尝试过 generate_artifact 就想
            # final → 纠偏一次让它先生成。已尝试过(哪怕失败)则放行,避免吞掉失败说明,
            # 也只纠偏一次,避免和模型死循环。
            if (
                required_artifact
                and not artifact_ids
                and not artifact_attempted
                and not artifact_nudged
            ):
                artifact_nudged = True
                messages.append(ChatMessage(role="assistant", content=raw))
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"用户明确要求产出 {required_artifact} 产物，但你还没有调用 "
                            f"generate_artifact 生成它。请先用 action='tool_call' 调用 "
                            f"generate_artifact（artifact_type='{required_artifact}'，"
                            "brief 写清结构与要点，基于上面已检索到的证据），生成成功后再 final。"
                        ),
                    )
                )
                continue
            answer = str(parsed.get("answer", "")).strip()
            if len(answer) < _MIN_FINAL_ANSWER_CHARS:
                answer = "(模型没有生成有效回答, 请重试或换个问法。)"
            async for delta in _chunked_deltas(answer):
                yield AgentEvent(type="delta", payload={"text": delta})
            yield AgentEvent(
                type="final",
                payload={
                    "answer": answer,
                    "artifact_ids": list(artifact_ids),
                    "citations": list(citations),
                },
            )
            yield AgentEvent(type="done")
            return

        # tool_call 分支
        tool_name = str(parsed.get("tool", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
        args = parsed.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        yield AgentEvent(
            type="tool_call",
            payload={"name": tool_name, "args": args, "reason": reason, "step": step},
        )

        handler = _TOOLS.get(tool_name)
        call_sig = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if handler is None:
            result = ToolResult(
                name=tool_name,
                ok=False,
                content=(
                    f"未知工具: '{tool_name}'。可用工具: {[*sorted(_TOOLS.keys()), 'final_answer']}。"
                    "final_answer 不是工具, 通过 action='final' 触发。"
                ),
                summary="未知工具",
            )
        elif call_sig in seen_calls:
            # 同一工具+完全相同参数已调过 → 不重复执行，提示模型换查询或直接收尾，
            # 避免模型卡在重复调用里把步数耗光（工具调用稳定性）。
            result = ToolResult(
                name=tool_name,
                ok=False,
                content=(
                    f"工具 {tool_name} 已用完全相同的参数调用过，结果不会变。"
                    "请换一个更具体的查询，或改用 action='final' 基于已有信息作答。"
                ),
                summary="重复调用已拦截",
            )
        else:
            if tool_name == "generate_artifact":
                artifact_attempted = True
            seen_calls.add(call_sig)
            result = await handler(args, deps)

        # 若该工具产出 artifact, 单独 emit 给前端
        art = result.metadata.get("artifact")
        if isinstance(art, dict) and art.get("artifact_id"):
            artifact_ids.append(str(art["artifact_id"]))
            yield AgentEvent(type="artifact", payload=art)

        _append_unique_citations(citations, result.metadata.get("citations"))

        yield AgentEvent(
            type="tool_result",
            payload={
                "name": result.name,
                "ok": result.ok,
                "summary": result.summary,
                "step": step,
            },
        )

        # 把这一步喂回 LLM 上下文
        messages.append(ChatMessage(role="assistant", content=raw))
        messages.append(
            ChatMessage(
                role="user",
                content=f"工具 {result.name} 结果 (ok={result.ok}):\n{result.content}",
            )
        )
        if result.ok and result.name in {"rag_search", "web_search"}:
            evidence_parts.append(f"[{result.name}]\n{result.content}")

    # max_iterations 用完仍未 final → 不要只抛错，强制让模型基于已有信息收个尾
    answer = await _forced_final_answer(main_llm, messages, evidence_parts, question)
    async for delta in _chunked_deltas(answer):
        yield AgentEvent(type="delta", payload={"text": delta})
    yield AgentEvent(
        type="final",
        payload={
            "answer": answer,
            "artifact_ids": list(artifact_ids),
            "citations": list(citations),
        },
    )
    yield AgentEvent(type="done")
