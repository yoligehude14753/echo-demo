"""RAG/Web 取证后回答，并对路由延迟与引用真实性做确定性约束。

Fast 通道只承担轻量分类：默认使用 Yunwu ``gpt-5.4-nano``，在 1～3 秒内
熔断；失败或输出非法时立即改用 Echo AI 主模型分类。检索失败不会阻断另一条
检索路径，但有证据时只允许输出带真实引用且能由所引证据支持的事实。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass

from app.memory.models import RecallResult
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.web_search import WebSearchPort
from app.schemas.llm import ChatMessage
from app.schemas.rag import RagChunk, RetrievalResult, WebHit

logger = logging.getLogger("echodesk.retrieve")

_CLASSIFIER_PROMPT = """你是路由器。给定一个用户问题，判断它适合从哪里取证据。
只能输出三个标签之一：
- rag：用户的问题与“本地知识库”（已上传的 PDF / 已结束的会议）有关，可在本地检索
- web：用户的问题涉及“最新”“今天”“目前”“新闻”“价格”“天气”等时效性强的内容，需要联网
- either：模糊或两者都可能

只输出标签，不要解释。"""

_ANSWER_PROMPT_TEMPLATE = """你是 EchoDesk，会议与办公场景的数字分身。

下面是检索到的证据。只能基于这些证据回答用户问题，并严格遵守：
1) 简洁分点，使用中文。
2) 每行只写一个事实或结论，并在同一行附上直接支持它的精确引用。
3) 本地引用只能原样使用 [doc:doc_id-chunk_id]，联网引用只能原样使用 [web:url]，记忆引用只能原样使用 [memory:candidate_id]；不得编造或改写引用 ID。
4) 有部分证据时，只回答证据覆盖的部分，静默省略其余部分；不得主动添加“未覆盖内容”“知识库里没找到”“缺失资料”或类似章节。
5) {gap_policy}
6) 一个引用不能支撑与其内容无关的数字、事实或结论；没有直接证据的内容不要输出。
7) “关联记忆”与当前问题直接匹配时，优先使用关联记忆；不得让主题无关的 RAG/Web 片段覆盖它。

---- 关联记忆 ----
{memory_block}

---- 证据（RAG） ----
{rag_block}

---- 证据（Web） ----
{web_block}

---- 用户问题 ----
{question}
"""

_WEB_HINTS = (
    "最新",
    "今天",
    "目前",
    "近期",
    "刚刚",
    "新闻",
    "价格",
    "天气",
    "汇率",
    "股价",
    "实时",
    "本周",
    "本月",
    "今年",
    "联网",
    "网页",
    "官网",
)
_RAG_HINTS = (
    "附件",
    "文档",
    "资料",
    "知识库",
    "会议",
    "纪要",
    "逐字稿",
    "上传",
    "本地",
    "文件",
    "刚才",
    "上次会议",
)
_GAP_MARKERS = (
    "缺口",
    "未覆盖",
    "没覆盖",
    "哪些没",
    "哪些没有",
    "还缺什么",
    "缺少什么",
    "资料不足",
    "证据不足",
    "缺失",
    "缺失资料",
    "没找到",
    "未提及",
    "没有提及",
    "遗漏",
)
_FORBIDDEN_GAP_MARKERS = (
    "已有证据中未覆盖",
    "已有资料中未覆盖",
    "未覆盖内容",
    "知识库里没找到",
    "知识库中没找到",
    "已有资料里没找到",
    "已有的资料里没找到",
    "没有找到相关内容",
    "知识库未找到",
    "知识库没有找到",
    "现有证据未包含",
    "现有资料未包含",
    "没有相关证据",
    "无相关证据",
    "未提供相关",
    "未提及",
    "没有提及",
    "缺乏资料",
    "暂无资料",
    "找不到相关",
    "缺失",
    "缺失资料",
    "缺少资料",
    "资料不足",
    "证据不足",
    "依据不足",
)
_ZERO_EVIDENCE_ANSWER = "当前没有足够的可用证据。"
_CITATION_RE = re.compile(r"\[(?:doc|web|memory):[^\]\r\n]+\]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_ENGLISH_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_CHINESE_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STRUCTURAL_LINES = {
    "根据现有证据：",
    "根据现有证据，",
    "现有证据显示：",
    "回答如下：",
    "---",
}
_STRUCTURAL_HEADINGS = {
    "回答",
    "核心结论",
    "结论",
    "要点",
    "关键信息",
    "主要信息",
    "证据摘要",
    "概览",
    "总结",
}
_GENERIC_SUPPORT_TOKENS = {
    "根据",
    "证据",
    "资料",
    "显示",
    "指出",
    "报告",
    "相关",
    "内容",
    "目前",
    "已经",
    "可以",
    "可能",
    "主要",
    "其中",
    "公司",
    "问题",
}
_MAIN_CLASSIFIER_TIMEOUT_S = 8.0


@dataclass
class AnswerStream:
    """retrieve_and_answer 的产物：检索结果 + SSE 答复流。"""

    retrieval: RetrievalResult
    chunks: AsyncIterator[str]
    memory: RecallResult | None = None


def _doc_citation(chunk: RagChunk) -> str:
    return f"[doc:{chunk.doc_id}-{chunk.chunk_id}]"


def _format_rag(chunks: list[RagChunk]) -> str:
    if not chunks:
        return "(无)"
    lines: list[str] = []
    for chunk in chunks[:5]:
        source_details = [chunk.doc_title]
        if page := chunk.metadata.get("page"):
            source_details.append(f"p{page}")
        details = " / ".join(item for item in source_details if item)
        lines.append(f"{_doc_citation(chunk)}（{details}）\n{chunk.text}")
    return "\n\n".join(lines)


def _format_web(hits: list[WebHit]) -> str:
    if not hits:
        return "(无)"
    return "\n\n".join(f"[web:{hit.url}] {hit.title}\n{hit.snippet}" for hit in hits[:5])


def _memory_citation(candidate_id: str) -> str:
    return f"[memory:{candidate_id}]"


def _format_memory(result: RecallResult | None) -> str:
    if result is None or not result.matches:
        return "(无)"
    return "\n\n".join(
        (
            f"{_memory_citation(match.candidate.candidate_id)} "
            f"（{match.candidate.level} / {match.candidate.kind} / "
            f"{match.candidate.source_ref}）\n{match.candidate.content}"
        )
        for match in result.matches
    )


def _deterministic_source(question: str) -> str | None:
    normalized = question.lower()
    wants_web = any(marker in normalized for marker in _WEB_HINTS)
    wants_rag = any(marker in normalized for marker in _RAG_HINTS)
    if wants_web and wants_rag:
        return "either"
    if wants_web:
        return "web"
    if wants_rag:
        return "rag"
    return None


def _parse_classifier_output(content: str | None) -> str | None:
    match = re.search(r"\b(rag|web|either)\b", (content or "").strip().lower())
    return match.group(1) if match else None


async def _classify(
    fast_llm: LLMPort,
    fast_model: str,
    question: str,
    *,
    fallback_llm: LLMPort | None = None,
    fallback_model: str | None = None,
    fast_timeout_s: float = 2.0,
) -> str:
    """先做零延迟规则路由，再以短熔断调用小模型，失败时切主模型。"""

    route_started = time.perf_counter()
    if deterministic := _deterministic_source(question):
        logger.info(
            "latency stage=route source=deterministic label=%s elapsed_ms=%.1f",
            deterministic,
            (time.perf_counter() - route_started) * 1000,
        )
        return deterministic

    clamped_timeout = min(3.0, max(1.0, float(fast_timeout_s)))
    try:
        fast_started = time.perf_counter()
        response = await fast_llm.chat(
            [
                ChatMessage(role="system", content=_CLASSIFIER_PROMPT),
                ChatMessage(role="user", content=question),
            ],
            model=fast_model,
            max_tokens=20,
            temperature=0.0,
            timeout_s=clamped_timeout,
        )
        label = _parse_classifier_output(response.content)
        logger.info(
            "latency stage=route source=fast model=%s valid=%s elapsed_ms=%.1f",
            fast_model,
            bool(label),
            (time.perf_counter() - fast_started) * 1000,
        )
        if label:
            return label
    except Exception as exc:
        logger.warning(
            "latency stage=route source=fast model=%s status=failed error_type=%s elapsed_ms=%.1f",
            fast_model,
            type(exc).__name__,
            (time.perf_counter() - route_started) * 1000,
        )

    if fallback_llm is not None:
        fallback_started = time.perf_counter()
        try:
            response = await fallback_llm.chat(
                [
                    ChatMessage(role="system", content=_CLASSIFIER_PROMPT),
                    ChatMessage(role="user", content=question),
                ],
                model=fallback_model,
                max_tokens=20,
                temperature=0.0,
                timeout_s=_MAIN_CLASSIFIER_TIMEOUT_S,
            )
            label = _parse_classifier_output(response.content)
            logger.info(
                "latency stage=route source=main_fallback model=%s valid=%s elapsed_ms=%.1f",
                fallback_model or "default",
                bool(label),
                (time.perf_counter() - fallback_started) * 1000,
            )
            if label:
                return label
        except Exception as exc:
            logger.warning(
                "latency stage=route source=main_fallback model=%s status=failed "
                "error_type=%s elapsed_ms=%.1f",
                fallback_model or "default",
                type(exc).__name__,
                (time.perf_counter() - fallback_started) * 1000,
            )

    logger.info(
        "latency stage=route source=degraded label=either elapsed_ms=%.1f",
        (time.perf_counter() - route_started) * 1000,
    )
    return "either"


def _asks_for_gaps(question: str) -> bool:
    normalized = question.lower()
    return any(marker in normalized for marker in _GAP_MARKERS)


def _evidence_sources(
    rag_chunks: list[RagChunk],
    web_hits: list[WebHit],
) -> dict[str, str]:
    sources = {_doc_citation(chunk): chunk.text for chunk in rag_chunks[:5] if chunk.text.strip()}
    sources.update(
        {
            f"[web:{hit.url}]": f"{hit.title}\n{hit.snippet}"
            for hit in web_hits[:5]
            if hit.title.strip() or hit.snippet.strip()
        }
    )
    return sources


def _memory_evidence_sources(result: RecallResult | None) -> dict[str, str]:
    if result is None:
        return {}
    return {
        _memory_citation(match.candidate.candidate_id): match.candidate.content
        for match in result.matches
        if match.candidate.content.strip()
    }


def _contains_gap_language(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(marker in normalized for marker in _GAP_MARKERS)


def _contains_forbidden_gap_language(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(marker in normalized for marker in _FORBIDDEN_GAP_MARKERS)


def _claim_clauses(line: str) -> list[str]:
    without_citations = _CITATION_RE.sub("", line)
    without_markdown = re.sub(r"^[\s>*#\-+\d.)]+", "", without_citations)
    return [part.strip() for part in re.split(r"[。；;！？!?]+", without_markdown) if part.strip()]


def _support_tokens(text: str) -> set[str]:
    tokens = {word.lower() for word in _ENGLISH_WORD_RE.findall(text)}
    for run in _CHINESE_RUN_RE.findall(text):
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return {token for token in tokens if token not in _GENERIC_SUPPORT_TOKENS}


def _clause_supported(clause: str, source_text: str) -> bool:
    normalized_source = source_text.lower().replace(",", "")
    for number in _NUMBER_RE.findall(clause):
        if number.lower().replace(",", "") not in normalized_source:
            return False

    claim_tokens = _support_tokens(clause)
    if not claim_tokens:
        return True
    source_tokens = _support_tokens(source_text)
    overlap = claim_tokens & source_tokens
    required_overlap = 2 if len(claim_tokens) >= 5 else 1
    return len(overlap) >= required_overlap


def _line_supported(line: str, citations: list[str], sources: dict[str, str]) -> bool:
    cited_text = "\n".join(sources[citation] for citation in citations)
    clauses = _claim_clauses(line)
    return bool(clauses) and all(_clause_supported(clause, cited_text) for clause in clauses)


def _is_structural_line(stripped: str) -> bool:
    if not stripped or stripped in _STRUCTURAL_LINES:
        return True
    if stripped.startswith("#"):
        heading = stripped.lstrip("# ").rstrip("：:").strip()
        heading = re.sub(r"^(?:[一二三四五六七八九十]+|\d+)[、.)]\s*", "", heading)
        return heading in _STRUCTURAL_HEADINGS
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", stripped))


def _fallback_evidence_line(sources: dict[str, str]) -> str:
    citation, raw_text = next(iter(sources.items()))
    excerpt = " ".join(raw_text.split())
    if len(excerpt) > 280:
        excerpt = f"{excerpt[:280].rstrip('，,；; ')}…"
    return f"- {excerpt} {citation}"


@dataclass
class _EvidenceGuard:
    sources: dict[str, str]
    allow_gaps: bool
    suppress_gap_section: bool = False
    explicit_gap_section: bool = False
    emitted_content: bool = False

    def process_line(self, line: str) -> str | None:  # noqa: PLR0911
        stripped = line.strip()
        is_heading = stripped.startswith("#")

        if self.allow_gaps and is_heading:
            self.explicit_gap_section = _contains_gap_language(stripped)

        if not self.allow_gaps:
            if is_heading:
                if _contains_forbidden_gap_language(stripped):
                    self.suppress_gap_section = True
                    return None
                self.suppress_gap_section = False
            elif self.suppress_gap_section:
                return None
            if _contains_forbidden_gap_language(stripped):
                return None

        if _is_structural_line(stripped):
            return line.rstrip()

        citations = _CITATION_RE.findall(line)
        if any(citation not in self.sources for citation in citations):
            return None

        if (
            self.allow_gaps
            and not citations
            and (self.explicit_gap_section or _contains_gap_language(stripped))
        ):
            self.emitted_content = True
            return line.rstrip()

        if not citations or not _line_supported(line, citations, self.sources):
            return None

        self.emitted_content = True
        return line.rstrip()

    def fallback(self) -> str | None:
        if self.emitted_content or not self.sources:
            return None
        self.emitted_content = True
        return _fallback_evidence_line(self.sources)


async def retrieve_and_answer(  # noqa: PLR0915
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
    main_model: str | None = None,
    fast_timeout_s: float = 2.0,
    memory_recall: Awaitable[RecallResult] | None = None,
) -> AnswerStream:
    """分类、并行检索并生成只含可追溯事实的回答。"""

    cls = await _classify(
        fast_llm,
        fast_model,
        question,
        fallback_llm=main_llm,
        fallback_model=main_model,
        fast_timeout_s=fast_timeout_s,
    )

    async def _retrieve_rag() -> list[RagChunk]:
        started = time.perf_counter()
        if cls not in {"rag", "either"}:
            logger.info("latency stage=retrieve status=skipped count=0 elapsed_ms=0.0")
            return []
        try:
            chunks = await rag.query(question, top_k=rag_top_k)
            logger.info(
                "latency stage=retrieve status=ok count=%d elapsed_ms=%.1f",
                len(chunks),
                (time.perf_counter() - started) * 1000,
            )
            return chunks
        except Exception as exc:
            logger.warning(
                "latency stage=retrieve status=failed count=0 error_type=%s elapsed_ms=%.1f",
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            return []

    async def _retrieve_web() -> list[WebHit]:
        started = time.perf_counter()
        if cls not in {"web", "either"}:
            logger.info("latency stage=web status=skipped count=0 elapsed_ms=0.0")
            return []
        try:
            hits = await web.search(question, top_n=web_top_n)
            logger.info(
                "latency stage=web status=ok count=%d elapsed_ms=%.1f",
                len(hits),
                (time.perf_counter() - started) * 1000,
            )
            return hits
        except Exception as exc:
            logger.warning(
                "latency stage=web status=failed count=0 error_type=%s elapsed_ms=%.1f",
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            return []

    async def _retrieve_memory() -> RecallResult | None:
        if memory_recall is None:
            return None
        started = time.perf_counter()
        try:
            result = await memory_recall
            logger.info(
                "latency stage=memory status=ok count=%d elapsed_ms=%.1f",
                len(result.matches),
                (time.perf_counter() - started) * 1000,
            )
            return result
        except Exception as exc:
            logger.warning(
                "latency stage=memory status=failed count=0 error_type=%s elapsed_ms=%.1f",
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            return None

    rag_chunks, web_hits, memory_result = await asyncio.gather(
        _retrieve_rag(),
        _retrieve_web(),
        _retrieve_memory(),
    )

    if rag_chunks and web_hits:
        chosen = "both"
    elif rag_chunks:
        chosen = "rag"
    elif web_hits:
        chosen = "web"
    else:
        chosen = "none"
    if memory_result is not None and memory_result.matches:
        chosen = "memory" if chosen == "none" else f"memory+{chosen}"
    retrieval = RetrievalResult(
        query=question,
        rag_chunks=rag_chunks,
        web_hits=web_hits,
        arbitration={"classifier": 1.0 if cls in {"rag", "web"} else 0.5},
        chosen_source=chosen,
    )

    sources = _memory_evidence_sources(memory_result)
    sources.update(_evidence_sources(rag_chunks, web_hits))
    allow_gaps = _asks_for_gaps(question)
    prompt = _ANSWER_PROMPT_TEMPLATE.format(
        memory_block=_format_memory(memory_result),
        rag_block=_format_rag(rag_chunks),
        web_block=_format_web(web_hits),
        question=question,
        gap_policy=(
            "用户明确询问了证据缺口，可以说明哪些方面没有覆盖。"
            if allow_gaps
            else "用户没有询问证据缺口，不得说明哪些方面没有覆盖。"
        ),
    )

    async def _gen() -> AsyncIterator[str]:  # noqa: PLR0912, PLR0915
        render_started = time.perf_counter()
        if not sources:
            logger.info(
                "latency stage=llm status=skipped reason=no_evidence model=%s elapsed_ms=0.0",
                main_model or "default",
            )
            yield _ZERO_EVIDENCE_ANSWER
            logger.info(
                "latency stage=render status=ok mode=zero_evidence elapsed_ms=%.1f",
                (time.perf_counter() - render_started) * 1000,
            )
            return

        guard = _EvidenceGuard(sources=sources, allow_gaps=allow_gaps)
        messages = [ChatMessage(role="user", content=prompt)]
        llm_started = time.perf_counter()
        render_ms = 0.0
        try:
            if not stream:
                response = await main_llm.chat(
                    messages,
                    model=main_model,
                    max_tokens=768,
                    temperature=0.2,
                    timeout_s=60.0,
                )
                logger.info(
                    "latency stage=llm status=ok model=%s elapsed_ms=%.1f",
                    main_model or "default",
                    (time.perf_counter() - llm_started) * 1000,
                )
                rendered: list[str] = []
                for line in (response.content or "").splitlines():
                    line_started = time.perf_counter()
                    if guarded := guard.process_line(line):
                        rendered.append(guarded)
                    render_ms += (time.perf_counter() - line_started) * 1000
                if fallback := guard.fallback():
                    rendered.append(fallback)
                yield "\n".join(rendered)
                logger.info(
                    "latency stage=render status=ok mode=buffered elapsed_ms=%.1f",
                    render_ms,
                )
                return

            upstream = main_llm.chat_stream(
                messages,
                model=main_model,
                max_tokens=768,
                temperature=0.2,
                timeout_s=60.0,
            )
            buffer = ""
            try:
                async for chunk in upstream:
                    if not chunk:
                        continue
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line_started = time.perf_counter()
                        guarded = guard.process_line(line)
                        render_ms += (time.perf_counter() - line_started) * 1000
                        if guarded is not None:
                            yield f"{guarded}\n"
            finally:
                close = getattr(upstream, "aclose", None)
                if callable(close):
                    await close()

            if buffer:
                line_started = time.perf_counter()
                guarded = guard.process_line(buffer)
                render_ms += (time.perf_counter() - line_started) * 1000
                if guarded is not None:
                    yield guarded
            if fallback := guard.fallback():
                yield fallback
            logger.info(
                "latency stage=llm status=ok model=%s elapsed_ms=%.1f",
                main_model or "default",
                (time.perf_counter() - llm_started) * 1000,
            )
            logger.info(
                "latency stage=render status=ok mode=stream elapsed_ms=%.1f",
                render_ms,
            )
        except Exception as exc:
            logger.warning(
                "latency stage=llm status=failed model=%s error_type=%s elapsed_ms=%.1f",
                main_model or "default",
                type(exc).__name__,
                (time.perf_counter() - llm_started) * 1000,
            )
            raise

    return AnswerStream(retrieval=retrieval, chunks=_gen(), memory=memory_result)
