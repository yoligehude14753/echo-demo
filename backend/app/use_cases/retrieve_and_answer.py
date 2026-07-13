"""use_case: retrieve_and_answer — RAG-grounded 问答（取代 PR-2 的 ask_question 朴素版）。

流程：
  1) Fast 通道分类器判别 query 类型：
     - "rag"：本地知识库可答（PDF/会议）
     - "web"：需联网（最新资讯/时事/价格）
     - "either"：两边都试
  2) 按分类执行 RAG / Web / 都跑
  3) 把检索结果拼到 prompt，用 MAIN 通道生成最终答复；调用方可选择一次性或真增量流

约束：
- 仅依赖 ports.LLMPort / ports.RagPort / ports.WebSearchPort
- 检索失败不打断流程：拼空字符串 + 答复中说明"无相关来源"
- fabrication_guard：MAIN prompt 显式要求"未在引用中找到 → 说不知道"
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

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

_ANSWER_PROMPT_TEMPLATE = """你是 EchoDesk，会议+办公场景的数字分身。

下面是检索到的证据（含本地知识库 RAG 和联网 Web）。请基于证据回答用户问题。
要求：
1) 简洁分点，中文
2) 引用证据时标 [doc:doc_id-chunk_id] 或 [web:url]
3) 如果证据中找不到答案 → 直接说 "在已有的资料里没找到相关内容"，不要编造
4) 不要凭空假设事实

---- 证据（RAG） ----
{rag_block}

---- 证据（Web） ----
{web_block}

---- 用户问题 ----
{question}
"""


@dataclass
class AnswerStream:
    """retrieve_and_answer 的产物：检索结果 + SSE 答复流。"""

    retrieval: RetrievalResult
    chunks: AsyncIterator[str]


def _format_rag(chunks: list[RagChunk]) -> str:
    if not chunks:
        return "(无)"
    lines: list[str] = []
    for c in chunks[:5]:
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


async def _classify(fast_llm: LLMPort, fast_model: str, question: str) -> str:
    # P2.3：fast LLM 失败时不让整条 RAG/web 链路 raise；退到 "either"
    # 让两条检索路径都跑，最终交给 main_llm 综合。public demo 下 fast
    # LLM 默认跟随 Yunwu fallback；私有部署改成 eight 时断连也算降级。
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


async def retrieve_and_answer(
    *,
    main_llm: LLMPort,
    fast_llm: LLMPort,
    fast_model: str,
    rag: RagPort,
    web: WebSearchPort,
    question: str,
    rag_top_k: int = 5,
    web_top_n: int = 5,
    stream: bool = False,
) -> AnswerStream:
    """先分类→检索→拼 prompt→生成答复。

    ``stream=False`` 保持原有一次性 ``chat()`` 行为；HTTP SSE 路径显式传
    ``stream=True``，直接转发 provider 的 async deltas。关闭或取消返回的
    ``chunks`` iterator 会在 ``finally`` 中关闭上游 stream。
    """
    cls = await _classify(fast_llm, fast_model, question)

    rag_chunks: list[RagChunk] = []
    web_hits: list[WebHit] = []
    try:
        if cls in {"rag", "either"}:
            rag_chunks = await rag.query(question, top_k=rag_top_k)
    except Exception:
        rag_chunks = []
    try:
        if cls in {"web", "either"}:
            web_hits = await web.search(question, top_n=web_top_n)
    except Exception:
        web_hits = []

    if rag_chunks and web_hits:
        chosen = "both"
    elif rag_chunks:
        chosen = "rag"
    elif web_hits:
        chosen = "web"
    else:
        chosen = "none"
    retrieval = RetrievalResult(
        query=question,
        rag_chunks=rag_chunks,
        web_hits=web_hits,
        arbitration={"classifier": 1.0 if cls in {"rag", "web"} else 0.5},
        chosen_source=chosen,
    )

    prompt = _ANSWER_PROMPT_TEMPLATE.format(
        rag_block=_format_rag(rag_chunks),
        web_block=_format_web(web_hits),
        question=question,
    )

    async def _gen() -> AsyncIterator[str]:
        try:
            messages = [ChatMessage(role="user", content=prompt)]
            if not stream:
                resp = await main_llm.chat(
                    messages,
                    max_tokens=768,
                    temperature=0.2,
                    timeout_s=60.0,
                )
                yield resp.content or "在已有的资料里没找到相关内容"
                return

            upstream = main_llm.chat_stream(
                messages,
                max_tokens=768,
                temperature=0.2,
                timeout_s=60.0,
            )
            emitted = False
            try:
                async for chunk in upstream:
                    if not chunk:
                        continue
                    emitted = True
                    yield chunk
            finally:
                close = getattr(upstream, "aclose", None)
                if callable(close):
                    await close()
            if not emitted:
                yield "在已有的资料里没找到相关内容"
        except Exception:
            logging.getLogger("echodesk.retrieve").warning("answer generation failed")
            raise

    return AnswerStream(retrieval=retrieval, chunks=_gen())
