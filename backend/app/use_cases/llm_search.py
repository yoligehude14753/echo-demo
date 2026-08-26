"""Pure Qwen hierarchical search over summaries and original text.

The persistence adapter only enumerates authorized documents and stores text;
it never ranks results. Qwen first searches every document synopsis, then reads
all original chunks of the selected documents in the largest safe live-context
batches. Synopses are discovery hints only; returned evidence always consists
of validated original chunks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.schemas.llm import ChatMessage
from app.schemas.rag import RagChunk

logger = logging.getLogger("echodesk.llm_search")

_SEARCH_TIMEOUT_S = 180.0
_SEARCH_CONCURRENCY = 2
_DOCUMENT_OUTPUT_TOKENS = 1_536
_CONTENT_OUTPUT_TOKENS = 2_048
_REDUCE_OUTPUT_TOKENS = 1_024
_CONTEXT_SAFETY_TOKENS = 2_048
_CHAT_FRAMING_TOKENS = 320

_DOCUMENT_SYSTEM = """ECHODESK_LLM_SEARCH_DOCUMENTS_V1
你是 EchoDesk 的文档语义搜索器。阅读本批次全部文档摘要，选择可能包含用户所需事实的文档。
只能输出严格 JSON，不要 Markdown：
{{"documents":[{{"doc_id":"原样 ID","summary":"该文档可能相关的语义理由"}}]}}
规则：
- 只能使用输入中存在的 doc_id，不得编造或改写。
- 这是宽召回阶段：只要摘要显示文档可能相关就应保留，不要在这里直接回答问题。
- 必须按语义、时间和事实关系判断；不要按词频、标题相似度或关键词数量排序。
- 不相关时输出 {{"documents":[]}}。
- 最多选择 {limit} 份文档。
"""

_DOCUMENT_REDUCE_SYSTEM = """ECHODESK_LLM_SEARCH_REDUCE_DOCUMENTS_V1
你是 EchoDesk 的文档候选合并器。根据用户问题，从各批次候选中保留最可能含有完整原始证据的文档。
只能输出严格 JSON，不要 Markdown：
{{"documents":[{{"doc_id":"原样 ID","summary":"保留该文档的语义理由"}}]}}
只能使用输入中存在的 doc_id，最多选择 {limit} 份文档。
"""

_CONTENT_SYSTEM = """ECHODESK_LLM_SEARCH_CONTENT_V1
你是 EchoDesk 的原文搜索器。阅读本批次全部原始片段，选择能直接回答用户问题、或构成完整回答所需部分事实的证据片段。
只能输出严格 JSON，不要 Markdown：
{{"chunks":[{{"chunk_id":"原样 ID","summary":"片段中与问题直接相关的事实摘要"}}]}}
规则：
- 只能使用输入中存在的 chunk_id，不得编造或改写。
- 必须按语义和事实匹配；不要按词频、片段长度或关键词数量排序。
- 数字、单位、专有名词必须以原文为准。
- 不相关时输出 {{"chunks":[]}}。
- 最多选择 {limit} 个片段。
"""

_CHUNK_REDUCE_SYSTEM = """ECHODESK_LLM_SEARCH_REDUCE_CHUNKS_V1
你是 EchoDesk 的证据合并器。根据用户问题，从各批次候选中选择最直接、信息最完整且互不重复的原始证据。
只能输出严格 JSON，不要 Markdown：
{{"chunks":[{{"chunk_id":"原样 ID","summary":"保留该证据的事实理由"}}]}}
只能使用输入中存在的 chunk_id，最多选择 {limit} 个。
"""


class LLMSearchError(RuntimeError):
    """The model-search result could not be validated against local evidence."""


@dataclass(frozen=True, slots=True)
class _Selection:
    item_id: str
    summary: str


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_json_object(content: str | None) -> dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    if start < 0:
        raise LLMSearchError("model search returned no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMSearchError("model search returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LLMSearchError("model search JSON must be an object")
    return value


def _validated_selections(
    payload: dict[str, Any],
    *,
    field: str,
    id_field: str,
    allowed_ids: set[str],
    limit: int,
    discard_unknown_ids: bool = False,
) -> list[_Selection]:
    raw_items = payload.get(field)
    if not isinstance(raw_items, list):
        raise LLMSearchError(f"model search JSON is missing {field}")
    if len(raw_items) > limit:
        raise LLMSearchError(f"model search returned too many {field}")
    selections: list[_Selection] = []
    seen: set[str] = set()
    discarded_unknown = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise LLMSearchError(f"model search {field} item must be an object")
        item_id = raw.get(id_field)
        summary = raw.get("summary", "")
        if not isinstance(item_id, str) or item_id not in allowed_ids:
            if discard_unknown_ids:
                discarded_unknown += 1
                continue
            raise LLMSearchError("model search returned an unknown evidence ID")
        if not isinstance(summary, str):
            raise LLMSearchError("model search summary must be text")
        if item_id in seen:
            continue
        seen.add(item_id)
        selections.append(_Selection(item_id=item_id, summary=summary.strip()[:2_000]))
    if discarded_unknown:
        logger.warning(
            "llm_search discarded unknown IDs field=%s count=%d",
            field,
            discarded_unknown,
        )
    return selections


async def _validated_with_one_repair(
    llm: LLMPort,
    *,
    model: str | None,
    payload: dict[str, Any],
    field: str,
    id_field: str,
    allowed_ids: set[str],
    limit: int,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> list[_Selection]:
    """Discard invented IDs; retry once only when the whole non-empty result is invalid."""

    selections = _validated_selections(
        payload,
        field=field,
        id_field=id_field,
        allowed_ids=allowed_ids,
        limit=limit,
        discard_unknown_ids=True,
    )
    if selections or not payload.get(field):
        return selections
    repaired = await _chat_json(
        llm,
        model=model,
        system_prompt=(
            f"{system_prompt}\n纠正要求：上次输出的 ID 全部无效。"
            f"本次只能逐字复制输入 JSON 的 {id_field}；不能缩写、改写或猜测。"
        ),
        user_prompt=user_prompt,
        max_tokens=max_tokens,
    )
    return _validated_selections(
        repaired,
        field=field,
        id_field=id_field,
        allowed_ids=allowed_ids,
        limit=limit,
        discard_unknown_ids=True,
    )


async def _context_limit(llm: LLMPort) -> int:
    resolver = getattr(llm, "resolve_chat_capability", None)
    if not callable(resolver):
        raise LLMSearchError("model capability is unavailable")
    try:
        capability = await resolver(timeout_s=30.0)
        value = int(getattr(capability, "context_window_tokens", 0) or 0)
        if value > 0:
            return value
    except Exception as exc:
        raise LLMSearchError("model capability is unavailable") from exc
    raise LLMSearchError("model capability has no context window")


def _record_budget(
    *,
    context_limit: int,
    output_tokens: int,
    system_prompt: str,
    user_prefix: str,
) -> int:
    fixed = len(system_prompt.encode("utf-8")) + len(user_prefix.encode("utf-8"))
    return max(
        1_024,
        context_limit
        - output_tokens
        - _CONTEXT_SAFETY_TOKENS
        - _CHAT_FRAMING_TOKENS
        - fixed,
    )


def _pack_lines(lines: list[str], budget: int) -> list[list[str]]:
    if not lines:
        return []
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1
        if size > budget:
            raise LLMSearchError("one search record exceeds the live model context budget")
        if current and used + size > budget:
            batches.append(current)
            current = []
            used = 0
        current.append(line)
        used += size
    if current:
        batches.append(current)
    return batches


async def _chat_json(
    llm: LLMPort,
    *,
    model: str | None,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await llm.chat(
                messages,
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                timeout_s=_SEARCH_TIMEOUT_S,
            )
            return _parse_json_object(response.content)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "llm_search model call failed attempt=%d error_type=%s",
                attempt + 1,
                type(exc).__name__,
            )
    raise LLMSearchError("model search failed after retry") from last_error


async def _map_batches(
    llm: LLMPort,
    *,
    model: str | None,
    system_prompt: str,
    user_prefix: str,
    batches: list[list[str]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)

    async def run(batch: list[str]) -> dict[str, Any]:
        async with semaphore:
            return await _chat_json(
                llm,
                model=model,
                system_prompt=system_prompt,
                user_prompt=f"{user_prefix}\n" + "\n".join(batch),
                max_tokens=max_tokens,
            )

    return list(await asyncio.gather(*(run(batch) for batch in batches)))


async def _hierarchical_reduce(
    llm: LLMPort,
    *,
    model: str | None,
    question: str,
    context_limit: int,
    selections: list[_Selection],
    target_limit: int,
    field: str,
    id_field: str,
    system_template: str,
    record_for: Callable[[_Selection], dict[str, Any]],
) -> list[_Selection]:
    """Reduce arbitrarily many batch summaries without exceeding live context."""

    current = list({selection.item_id: selection for selection in selections}.values())
    prefix = f"用户问题：{question}\n候选摘要（每行一个 JSON）："
    semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)
    while len(current) > target_limit:
        budget_system = system_template.format(limit=target_limit)
        lines = [_json_line(record_for(selection)) for selection in current]
        batches = _pack_lines(
            lines,
            _record_budget(
                context_limit=context_limit,
                output_tokens=_REDUCE_OUTPUT_TOKENS,
                system_prompt=budget_system,
                user_prefix=prefix,
            ),
        )

        async def reduce_batch(batch: list[str]) -> list[_Selection]:
            # Multiple packed batches form a semantic tournament. Halving each
            # batch guarantees progress; the final round selects target_limit.
            batch_limit = (
                target_limit
                if len(batches) == 1
                else min(target_limit, max(1, len(batch) // 2))
            )
            system_prompt = system_template.format(limit=batch_limit)
            async with semaphore:
                payload = await _chat_json(
                    llm,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=f"{prefix}\n" + "\n".join(batch),
                    max_tokens=_REDUCE_OUTPUT_TOKENS,
                )
            allowed = {json.loads(line)[id_field] for line in batch}
            return _validated_selections(
                payload,
                field=field,
                id_field=id_field,
                allowed_ids=allowed,
                limit=batch_limit,
            )

        previous_count = len(current)
        reduced = await asyncio.gather(*(reduce_batch(batch) for batch in batches))
        current = list(
            {
                selection.item_id: selection
                for group in reduced
                for selection in group
            }.values()
        )
        if len(current) >= previous_count:
            raise LLMSearchError("hierarchical model summary did not reduce candidates")
    return current


def _chunk_line(chunk: RagChunk) -> str:
    metadata = {
        key: value
        for key, value in chunk.metadata.items()
        if key in {"page", "kind", "meeting_id", "captured_at", "speaker_label", "source"}
    }
    return _json_line(
        {
            "doc_id": chunk.doc_id,
            "doc_title": chunk.doc_title,
            "chunk_id": chunk.chunk_id,
            "metadata": metadata,
            "text": chunk.text,
        }
    )


def _document_line(entry: RagDocumentEntry) -> str:
    return _json_line(
        {
            "doc_id": entry.doc_id,
            "title": entry.title,
            "source": entry.source,
            "kind": entry.kind,
            "updated_at": entry.updated_at,
            "n_chunks": entry.n_chunks,
            "synopsis": entry.preview,
        }
    )


async def _select_documents(
    *,
    llm: LLMPort,
    model: str | None,
    question: str,
    catalog: list[RagDocumentEntry],
    context_limit: int,
    target_limit: int,
) -> list[RagDocumentEntry]:
    if not catalog:
        return []
    per_batch_limit = min(24, max(8, target_limit * 2))
    system_prompt = _DOCUMENT_SYSTEM.format(limit=per_batch_limit)
    prefix = f"用户问题：{question}\n文档语义摘要（每行一个 JSON，必须全部阅读）："
    lines = [_document_line(entry) for entry in catalog]
    batches = _pack_lines(
        lines,
        _record_budget(
            context_limit=context_limit,
            output_tokens=_DOCUMENT_OUTPUT_TOKENS,
            system_prompt=system_prompt,
            user_prefix=prefix,
        ),
    )
    payloads = await _map_batches(
        llm,
        model=model,
        system_prompt=system_prompt,
        user_prefix=prefix,
        batches=batches,
        max_tokens=_DOCUMENT_OUTPUT_TOKENS,
    )
    selections: list[_Selection] = []
    for batch, payload in zip(batches, payloads, strict=True):
        allowed = {json.loads(line)["doc_id"] for line in batch}
        selections.extend(
            await _validated_with_one_repair(
                llm,
                model=model,
                payload=payload,
                field="documents",
                id_field="doc_id",
                allowed_ids=allowed,
                limit=per_batch_limit,
                system_prompt=system_prompt,
                user_prompt=f"{prefix}\n" + "\n".join(batch),
                max_tokens=_DOCUMENT_OUTPUT_TOKENS,
            )
        )
    by_doc_id = {entry.doc_id: entry for entry in catalog}
    selections = list(
        {selection.item_id: selection for selection in selections}.values()
    )
    if len(selections) > target_limit:
        selections = await _hierarchical_reduce(
            llm,
            model=model,
            question=question,
            context_limit=context_limit,
            selections=selections,
            target_limit=target_limit,
            field="documents",
            id_field="doc_id",
            system_template=_DOCUMENT_REDUCE_SYSTEM,
            record_for=lambda selection: {
                "doc_id": selection.item_id,
                "title": by_doc_id[selection.item_id].title,
                "source": by_doc_id[selection.item_id].source,
                "updated_at": by_doc_id[selection.item_id].updated_at,
                "candidate_summary": selection.summary[:480],
            },
        )
    return [by_doc_id[selection.item_id] for selection in selections[:target_limit]]


async def _select_chunks(
    *,
    llm: LLMPort,
    model: str | None,
    question: str,
    chunks: list[RagChunk],
    context_limit: int,
    top_k: int,
) -> list[RagChunk]:
    if not chunks:
        return []
    per_batch_limit = min(24, max(8, top_k * 3))
    system_prompt = _CONTENT_SYSTEM.format(limit=per_batch_limit)
    prefix = f"用户问题：{question}\n原始片段（每行一个 JSON，必须全部阅读）："
    lines = [_chunk_line(chunk) for chunk in chunks]
    batches = _pack_lines(
        lines,
        _record_budget(
            context_limit=context_limit,
            output_tokens=_CONTENT_OUTPUT_TOKENS,
            system_prompt=system_prompt,
            user_prefix=prefix,
        ),
    )
    payloads = await _map_batches(
        llm,
        model=model,
        system_prompt=system_prompt,
        user_prefix=prefix,
        batches=batches,
        max_tokens=_CONTENT_OUTPUT_TOKENS,
    )
    selections: list[_Selection] = []
    for batch, payload in zip(batches, payloads, strict=True):
        allowed = {json.loads(line)["chunk_id"] for line in batch}
        selections.extend(
            await _validated_with_one_repair(
                llm,
                model=model,
                payload=payload,
                field="chunks",
                id_field="chunk_id",
                allowed_ids=allowed,
                limit=per_batch_limit,
                system_prompt=system_prompt,
                user_prompt=f"{prefix}\n" + "\n".join(batch),
                max_tokens=_CONTENT_OUTPUT_TOKENS,
            )
        )
    by_chunk_id = {chunk.chunk_id: chunk for chunk in chunks}
    deduped: dict[str, _Selection] = {}
    for selection in selections:
        deduped.setdefault(selection.item_id, selection)
    selections = list(deduped.values())
    if len(selections) > top_k:
        selections = await _hierarchical_reduce(
            llm,
            model=model,
            question=question,
            context_limit=context_limit,
            selections=selections,
            target_limit=top_k,
            field="chunks",
            id_field="chunk_id",
            system_template=_CHUNK_REDUCE_SYSTEM,
            record_for=lambda selection: {
                "chunk_id": selection.item_id,
                "doc_id": by_chunk_id[selection.item_id].doc_id,
                "doc_title": by_chunk_id[selection.item_id].doc_title,
                "batch_summary": selection.summary[:240],
                "original_excerpt": by_chunk_id[selection.item_id].text[:320],
            },
        )

    evidence: list[RagChunk] = []
    for rank, selection in enumerate(selections[:top_k]):
        original = by_chunk_id[selection.item_id]
        evidence.append(original.model_copy(update={"score": max(0.0, 1.0 - rank * 0.01)}))
    return evidence


async def search_local_evidence(
    *,
    llm: LLMPort,
    model: str | None,
    rag: RagPort,
    question: str,
    top_k: int = 5,
    document_ids: list[str] | None = None,
) -> list[RagChunk]:
    """Search local evidence exclusively through the configured main LLM."""

    requested_top_k = min(20, max(1, int(top_k)))
    context_limit = await _context_limit(llm)
    if document_ids is not None:
        # The UI already knows which meeting the user is asking about.  This is
        # an authorization-preserving scope, not a relevance shortcut: the RAG
        # adapter still intersects IDs with the current principal, and Qwen
        # reads every original chunk in that scoped document before selecting
        # evidence.  No BM25/vector/embedding or local ranking is introduced.
        scoped_ids = list(
            dict.fromkeys(
                item.strip()
                for item in document_ids[:20]
                if isinstance(item, str) and item.strip()
            )
        )
        chunks = await rag.read_chunks(scoped_ids)
        evidence = await _select_chunks(
            llm=llm,
            model=model,
            question=question,
            chunks=chunks,
            context_limit=context_limit,
            top_k=requested_top_k,
        )
        logger.info(
            "llm_search status=ok scope=explicit context_limit=%d documents=%d "
            "source_chunks=%d evidence=%d",
            context_limit,
            len({chunk.doc_id for chunk in chunks}),
            len(chunks),
            len(evidence),
        )
        return evidence
    catalog = await rag.search_catalog()
    if not catalog:
        logger.info(
            "llm_search status=ok context_limit=%d catalog=%d documents=0 chunks=0",
            context_limit,
            len(catalog),
        )
        return []
    # Qwen reads the complete authorized synopsis catalog; there is no local
    # relevance score. It then verifies every selected document against its
    # original chunks before any evidence is returned.
    document_limit = min(20, max(8, requested_top_k * 2))
    selected_documents = await _select_documents(
        llm=llm,
        model=model,
        question=question,
        catalog=catalog,
        context_limit=context_limit,
        target_limit=document_limit,
    )
    chunks = await rag.read_chunks([entry.doc_id for entry in selected_documents])
    evidence = await _select_chunks(
        llm=llm,
        model=model,
        question=question,
        chunks=chunks,
        context_limit=context_limit,
        top_k=requested_top_k,
    )
    logger.info(
        "llm_search status=ok context_limit=%d catalog=%d documents=%d source_chunks=%d evidence=%d",
        context_limit,
        len(catalog),
        len(selected_documents),
        len(chunks),
        len(evidence),
    )
    return evidence


__all__ = ["LLMSearchError", "search_local_evidence"]
