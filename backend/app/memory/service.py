"""Bounded L0-L3 recall and small-model semantic memory extraction."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.memory.models import (
    ExtractionResult,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySourceKind,
    MemoryWriteCandidate,
    ProfileSettingRecord,
    ProvenanceInput,
    ProvenanceRecord,
    RecallCandidate,
    RecallMatch,
    RecallResult,
)
from app.memory.prompts import MEMORY_ASSOCIATION_PROMPT, MEMORY_EXTRACTION_PROMPT
from app.memory.ranking import lexical_relevance, prefilter_candidates
from app.memory.repository import MemoryRepository, normalize_text, utc_now
from app.memory.working import WorkingMemoryStore
from app.ports.llm import LLMPort
from app.schemas.llm import ChatMessage

logger = logging.getLogger("echodesk.memory")


def _strip_json_fence(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _safe_json(raw: str) -> dict[str, Any]:
    parsed = json.loads(_strip_json_fence(raw))
    if not isinstance(parsed, dict):
        raise ValueError("small-model output must be one JSON object")
    return parsed


def _bounded_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:300]}"


class MemoryService:
    """Workflow: parallel recall -> nano association -> grounded prompt context."""

    def __init__(self, settings: Settings, llm: LLMPort) -> None:
        self.settings = settings
        self.llm = llm
        self.repository = MemoryRepository(settings.db_path)
        self.working = WorkingMemoryStore(
            ttl_s=settings.memory_working_ttl_s,
            max_items=settings.memory_working_max_items,
            max_chars=settings.memory_working_max_chars,
        )
        self._tasks: set[asyncio.Task[Any]] = set()

    async def history_messages(
        self,
        scope: MemoryScope,
        conversation_id: str,
    ) -> list[ChatMessage]:
        return await self.working.history_messages(scope, conversation_id)

    async def recall(
        self,
        scope: MemoryScope,
        query: str,
        *,
        conversation_id: str = "default",
        limit: int | None = None,
    ) -> RecallResult:
        started = perf_counter()
        result_limit = limit or self.settings.memory_recall_limit
        layers = await asyncio.gather(
            self.working.candidates(scope, conversation_id),
            self.repository.current_meeting_candidates(
                scope,
                max_age_s=self.settings.memory_current_meeting_window_s,
                limit=self.settings.memory_current_meeting_max_segments,
            ),
            self.repository.episodic_candidates(
                scope,
                limit_per_kind=self.settings.memory_episodic_candidates_per_kind,
            ),
            self.repository.semantic_candidates(
                scope,
                limit=self.settings.memory_semantic_candidate_limit,
            ),
            self.repository.profile_candidates(scope),
            return_exceptions=True,
        )
        candidates = self._collect_layers(layers)
        shortlist = prefilter_candidates(
            query,
            candidates,
            limit=self.settings.memory_recall_prefilter_limit,
        )
        matches, used_model = await self._associate(query, shortlist, result_limit)
        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "memory recall layers=%d shortlist=%d matches=%d model=%s elapsed_ms=%.1f",
            len(candidates),
            len(shortlist),
            len(matches),
            used_model,
            elapsed_ms,
        )
        return RecallResult(
            query=query,
            matches=matches,
            used_small_model=used_model,
            small_model=self.settings.llm_fast_display_name if used_model else None,
            latency_ms=elapsed_ms,
        )

    @staticmethod
    def _collect_layers(layers: list[Any]) -> list[RecallCandidate]:
        candidates: list[RecallCandidate] = []
        for layer in layers:
            if isinstance(layer, BaseException):
                logger.warning("memory recall layer failed: %s", _bounded_error(layer))
                continue
            candidates.extend(item for item in layer if isinstance(item, RecallCandidate))
        seen: set[str] = set()
        return [item for item in candidates if not (item.candidate_id in seen or seen.add(item.candidate_id))]

    async def _associate(
        self,
        query: str,
        candidates: list[RecallCandidate],
        limit: int,
    ) -> tuple[list[RecallMatch], bool]:
        if not candidates:
            return [], False
        timeout_s = self.settings.memory_small_model_timeout_s
        system = MEMORY_ASSOCIATION_PROMPT.replace("{limit}", str(limit))
        payload = {
            "query": query,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "level": item.level,
                    "kind": item.kind,
                    "content": item.content[:1_500],
                    "source_ref": item.source_ref,
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in candidates
            ],
        }
        try:
            async with asyncio.timeout(timeout_s):
                response = await self.llm.chat(
                    [
                        ChatMessage(role="system", content=system),
                        ChatMessage(
                            role="user",
                            content=json.dumps(payload, ensure_ascii=False, default=str),
                        ),
                    ],
                    model=self.settings.llm_fast_model,
                    max_tokens=min(512, self.settings.llm_fast_max_tokens),
                    temperature=0.0,
                    timeout_s=timeout_s,
                )
            parsed = _safe_json(response.content)
            return self._validated_matches(parsed, candidates, limit), True
        except Exception as error:
            logger.warning("memory association fell back: %s", _bounded_error(error))
            return self._fallback_matches(query, candidates, limit), False

    @staticmethod
    def _validated_matches(
        parsed: dict[str, Any],
        candidates: list[RecallCandidate],
        limit: int,
    ) -> list[RecallMatch]:
        by_id = {item.candidate_id: item for item in candidates}
        output: list[RecallMatch] = []
        used: set[str] = set()
        raw_matches = parsed.get("matches")
        if not isinstance(raw_matches, list):
            raise ValueError("memory association misses matches[]")
        for raw in raw_matches:
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("candidate_id") or "")
            if candidate_id in used or candidate_id not in by_id:
                continue
            relevance = max(0.0, min(1.0, float(raw.get("relevance") or 0.0)))
            if relevance < 0.45:
                continue
            candidate = by_id[candidate_id]
            relation = str(raw.get("relation") or "与当前问题相关")[:300]
            output.append(
                RecallMatch(
                    candidate=candidate,
                    relevance=relevance,
                    relation=relation,
                    score=0.68 * relevance + 0.32 * candidate.deterministic_score,
                )
            )
            used.add(candidate_id)
            if len(output) >= limit:
                break
        return sorted(output, key=lambda item: item.score, reverse=True)

    @staticmethod
    def _fallback_matches(
        query: str,
        candidates: list[RecallCandidate],
        limit: int,
    ) -> list[RecallMatch]:
        matches: list[RecallMatch] = []
        for candidate in candidates:
            relevance = lexical_relevance(query, candidate.content)
            if relevance < 0.28:
                continue
            matches.append(
                RecallMatch(
                    candidate=candidate,
                    relevance=relevance,
                    relation="关键词与当前问题匹配",
                    score=0.68 * relevance + 0.32 * candidate.deterministic_score,
                )
            )
        return sorted(matches, key=lambda item: item.score, reverse=True)[:limit]

    async def remember_chat_turn(
        self,
        scope: MemoryScope,
        *,
        conversation_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        await self.working.append_turn(
            scope,
            conversation_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        self.schedule_extraction(
            scope,
            text=user_text,
            source_kind="conversation_user",
            source_id=turn_id,
            occurred_at=utc_now(),
            metadata={"conversation_id": conversation_id},
        )

    def schedule_extraction(
        self,
        scope: MemoryScope,
        *,
        text: str,
        source_kind: MemorySourceKind,
        source_id: str,
        occurred_at: datetime,
        source_segment_id: str | None = None,
        meeting_id: str | None = None,
        artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        task = asyncio.create_task(
            self.extract_text(
                scope,
                text=text,
                source_kind=source_kind,
                source_id=source_id,
                occurred_at=occurred_at,
                source_segment_id=source_segment_id,
                meeting_id=meeting_id,
                artifact_id=artifact_id,
                metadata=metadata,
            ),
            name=f"memory-extract:{source_kind}:{source_id[:48]}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._extraction_done)

    def _extraction_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("background memory extraction failed: %s", _bounded_error(error))

    async def extract_text(
        self,
        scope: MemoryScope,
        *,
        text: str,
        source_kind: MemorySourceKind,
        source_id: str,
        occurred_at: datetime,
        source_segment_id: str | None = None,
        meeting_id: str | None = None,
        artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        clean_text = text.strip()
        run_id = f"memrun_{uuid4().hex}"
        model = self.settings.llm_fast_model
        display_name = self.settings.llm_fast_display_name
        created_at = utc_now()
        if len(normalize_text(clean_text)) < self.settings.memory_extraction_min_chars:
            result = ExtractionResult(
                run_id=run_id,
                source_id=source_id,
                state="skipped",
                model=model,
                model_display_name=display_name,
                error="source_too_short",
            )
            await self._record_extraction(scope, result, source_kind, clean_text, created_at, [])
            return result
        existing = await self.repository.semantic_candidates(
            scope,
            limit=self.settings.memory_extraction_existing_limit,
        )
        await self.repository.record_extraction_run(
            scope,
            run_id=run_id,
            source_kind=source_kind,
            source_id=source_id,
            input_sha256=sha256(clean_text.encode("utf-8")).hexdigest(),
            model=model,
            model_display_name=display_name,
            state="running",
            latency_ms=0.0,
            candidate_count=0,
            output={},
            error=None,
            created_at=created_at,
        )
        started = perf_counter()
        try:
            raw_candidates = await self._extract_candidates(clean_text, existing)
            validated, rejected = self._validate_candidates(raw_candidates, clean_text, existing)
            memories = await self._persist_candidates(
                scope,
                validated,
                source_kind=source_kind,
                source_id=source_id,
                occurred_at=occurred_at,
                source_segment_id=source_segment_id,
                meeting_id=meeting_id,
                artifact_id=artifact_id,
                metadata=metadata or {},
            )
            result = ExtractionResult(
                run_id=run_id,
                source_id=source_id,
                state="succeeded",
                memories=memories,
                rejected_count=rejected,
                model=model,
                model_display_name=display_name,
                latency_ms=(perf_counter() - started) * 1000,
            )
        except Exception as error:
            result = ExtractionResult(
                run_id=run_id,
                source_id=source_id,
                state="failed",
                model=model,
                model_display_name=display_name,
                latency_ms=(perf_counter() - started) * 1000,
                error=_bounded_error(error),
            )
        await self._record_extraction(
            scope,
            result,
            source_kind,
            clean_text,
            created_at,
            {"memory_ids": [item.memory_id for item in result.memories]},
        )
        return result

    async def _extract_candidates(
        self,
        text: str,
        existing: list[RecallCandidate],
    ) -> list[Any]:
        limit = self.settings.memory_extraction_max_items
        prompt = (
            MEMORY_EXTRACTION_PROMPT.replace("{limit}", str(limit)).replace(
                "{min_confidence}",
                str(self.settings.memory_min_confidence),
            )
        )
        payload = {
            "INPUT": text,
            "EXISTING": [
                {
                    "memory_id": item.memory_id,
                    "kind": item.kind,
                    "content": item.content,
                    "canonical_key": item.metadata.get("canonical_key"),
                }
                for item in existing
                if item.memory_id
            ],
        }
        timeout_s = self.settings.memory_small_model_timeout_s
        async with asyncio.timeout(timeout_s):
            response = await self.llm.chat(
                [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(
                        role="user",
                        content=json.dumps(payload, ensure_ascii=False),
                    ),
                ],
                model=self.settings.llm_fast_model,
                max_tokens=self.settings.llm_fast_max_tokens,
                temperature=0.0,
                timeout_s=timeout_s,
            )
        parsed = _safe_json(response.content)
        raw = parsed.get("memories")
        if not isinstance(raw, list):
            raise ValueError("memory extraction misses memories[]")
        return raw[:limit]

    def _validate_candidates(
        self,
        raw_candidates: list[Any],
        source_text: str,
        existing: list[RecallCandidate],
    ) -> tuple[list[MemoryWriteCandidate], int]:
        valid_ids = {item.memory_id for item in existing if item.memory_id}
        normalized_source = normalize_text(source_text)
        output: list[MemoryWriteCandidate] = []
        rejected = 0
        for raw in raw_candidates:
            try:
                candidate = MemoryWriteCandidate.model_validate(raw)
                if candidate.confidence < self.settings.memory_min_confidence:
                    raise ValueError("confidence below threshold")
                if normalize_text(candidate.evidence_quote) not in normalized_source:
                    raise ValueError("evidence quote is not present in source")
                if candidate.existing_memory_id not in valid_ids:
                    candidate.existing_memory_id = None
                    if candidate.action == "supersede":
                        raise ValueError("supersede target is not an active memory")
                candidate.relation_memory_ids = [
                    item for item in candidate.relation_memory_ids if item in valid_ids
                ]
                output.append(candidate)
            except (TypeError, ValueError):
                rejected += 1
        return output, rejected

    async def _persist_candidates(
        self,
        scope: MemoryScope,
        candidates: list[MemoryWriteCandidate],
        *,
        source_kind: MemorySourceKind,
        source_id: str,
        occurred_at: datetime,
        source_segment_id: str | None,
        meeting_id: str | None,
        artifact_id: str | None,
        metadata: dict[str, Any],
    ) -> list[MemoryRecord]:
        memories: list[MemoryRecord] = []
        for candidate in candidates:
            record = await self.repository.upsert_candidate(
                scope,
                candidate,
                ProvenanceInput(
                    source_kind=source_kind,
                    source_id=source_id,
                    source_segment_id=source_segment_id,
                    meeting_id=meeting_id,
                    artifact_id=artifact_id,
                    excerpt=candidate.evidence_quote,
                    confidence=candidate.confidence,
                    occurred_at=occurred_at,
                    metadata=metadata,
                ),
            )
            if record is not None:
                memories.append(record)
        return memories

    async def _record_extraction(
        self,
        scope: MemoryScope,
        result: ExtractionResult,
        source_kind: MemorySourceKind,
        source_text: str,
        created_at: datetime,
        output: Any,
    ) -> None:
        await self.repository.record_extraction_run(
            scope,
            run_id=result.run_id,
            source_kind=source_kind,
            source_id=result.source_id,
            input_sha256=sha256(source_text.encode("utf-8")).hexdigest(),
            model=result.model,
            model_display_name=result.model_display_name,
            state=result.state,
            latency_ms=result.latency_ms,
            candidate_count=len(result.memories),
            output=output,
            error=result.error,
            created_at=created_at,
        )

    async def list_nodes(
        self,
        scope: MemoryScope,
        *,
        query: str | None = None,
        kind: MemoryKind | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return await self.repository.list_nodes(
            scope,
            query=query,
            kind=kind,
            include_deleted=include_deleted,
            limit=limit,
        )

    async def get_node(self, scope: MemoryScope, memory_id: str) -> MemoryRecord | None:
        return await self.repository.get_node(scope, memory_id)

    async def provenance(
        self,
        scope: MemoryScope,
        memory_id: str,
    ) -> list[ProvenanceRecord]:
        return await self.repository.list_provenance(scope, memory_id)

    async def confirm_node(
        self,
        scope: MemoryScope,
        memory_id: str,
    ) -> MemoryRecord | None:
        return await self.repository.confirm_node(scope, memory_id)

    async def update_node(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        content: str | None = None,
        kind: MemoryKind | None = None,
        canonical_key: str | None = None,
        salience: float | None = None,
        memory_scope: str | None = None,
    ) -> MemoryRecord | None:
        return await self.repository.update_node(
            scope,
            memory_id,
            content=content,
            kind=kind,
            canonical_key=canonical_key,
            salience=salience,
            memory_scope=memory_scope,
        )

    async def delete_node(self, scope: MemoryScope, memory_id: str) -> bool:
        return await self.repository.delete_node(scope, memory_id)

    async def profile(self, scope: MemoryScope) -> list[ProfileSettingRecord]:
        return await self.repository.list_profile_settings(scope)

    async def upsert_profile_setting(
        self,
        scope: MemoryScope,
        config_key: str,
        value: Any,
        *,
        description: str | None = None,
    ) -> ProfileSettingRecord:
        return await self.repository.upsert_profile_setting(
            scope,
            config_key,
            value,
            description=description,
        )

    async def delete_profile_setting(self, scope: MemoryScope, config_key: str) -> bool:
        return await self.repository.delete_profile_setting(scope, config_key)

    async def clear_working(self, scope: MemoryScope, conversation_id: str) -> bool:
        return await self.working.clear(scope, conversation_id)

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self.working.aclose()


_singleton: MemoryService | None = None
_singleton_key: tuple[str, int, str] | None = None


def get_memory_service(settings: Settings, llm: LLMPort) -> MemoryService:
    global _singleton, _singleton_key  # noqa: PLW0603
    key = (str(Path(settings.db_path).expanduser()), id(llm), settings.llm_fast_model)
    if _singleton is None or _singleton_key != key:
        _singleton = MemoryService(settings, llm)
        _singleton_key = key
    return _singleton


async def aclose_memory_service() -> None:
    global _singleton, _singleton_key  # noqa: PLW0603
    if _singleton is not None:
        await _singleton.aclose()
    _singleton = None
    _singleton_key = None


__all__ = ["MemoryService", "aclose_memory_service", "get_memory_service"]
