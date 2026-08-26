"""会议 Pipeline UseCase：转写 → 声纹归属 → 纪要 → RAG 入库。

设计要点（PRD M2-T2）：
- ``add_chunk(meeting_id, audio_bytes)``：会议进行中按 chunk 调用，返回 ``TranscriptSegment``
  - STT.transcribe + Diarizer.identify 并发执行（声纹用 chunk 整段做 enrollment）
  - speaker_id 由 diarizer 注册的 speaker_1 / speaker_2 … 决定，label 给可读名
- ``finalize_meeting(meeting_id, title)``：会议结束触发
  - 拼接所有 segments → 用 MAIN LLM 生成结构化 ``MeetingMinutes``
  - 把纪要+逐字稿写到 RAG（同 doc_id 一次性入库）
  - 落盘原始 transcript JSON（断电恢复）

PRD 验收约束：
- LLM 失败 → 抛错给上层，不返回半成品纪要
- 一个 chunk 哪怕 STT 段为空也不阻塞下一 chunk（产品化：会议讲话有间隙）
- 短片段（< 4s）声纹回退到现有 speaker，不注册新人（adapter 层已处理）
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.ports.diarizer import DiarizerPort
from app.ports.event_bus import EventBusPort
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.repository import MeetingRecord, RepositoryPort
from app.ports.stt import STTPort
from app.schemas.events import EchoEvent
from app.schemas.llm import ChatMessage
from app.schemas.meeting import MeetingMinutes, MinutesSection, TodoItem, TranscriptSegment
from app.security.scope import physical_resource_id, scoped_directory
from app.use_cases.minutes_budget import calculate_minutes_max_tokens

logger = logging.getLogger("echodesk.meeting_pipeline")

# M_minutes_refactor（2026-05-28）：把以前只返「summary/sections/decisions/
# action_items」的 prompt 升级为同时返「title（语义化标题，≤18 字中文）+ todos
# （含 assignee/kind/suggested_command）」的单 JSON。
#
# 能放进模型上下文时只调用一次；长逐字稿先做有界事实压缩，再由最后一次调用
# 统一生成 JSON，避免截断原文或让 title/sections/todos 失去全局上下文。
_MINUTES_SYS_PROMPT = """你是会议纪要助手。基于以下逐字稿生成**结构化中文纪要**，严格输出 JSON：

```json
{
  "title": "≤18 字的语义化中文标题，概括本次会议主题",
  "summary": "2-3 句话核心结论",
  "sections": [
    {"heading": "议题1标题", "bullets": ["要点1", "要点2"]}
  ],
  "decisions": ["明确做出的决定"],
  "todos": [
    {
      "text": "具体待办描述（例如：生成 Q3 销售拆解 PPT）",
      "assignee": "说话人1",
      "kind": "actionable",
      "suggested_command": "@生成 PPT Q3 销售拆解"
    }
  ]
}
```

要求：
1. 不要照抄逐字稿，提炼要点
2. 决议和待办必须真实出现在原文，不要编造
3. sections 按议题切分，每个 ≥ 2 个 bullets
4. title 必须能让一个没参会的人一眼看懂今天讲了什么（例：「直播带货话术 + AI 编程营销讨论」），禁止用「会议纪要 / 第 N 次例会 / 未命名会议」这类无信息标题
5. todos 抽取规则：
   - 抽出所有「行动项 / 待办」，每条带：
     - text：一句话描述
     - assignee：用对话里的「说话人 N」标签或人名；找不到具体人填 null
     - kind：含「生成 PPT / 做表 / 查资料 / 发邮件 / 计算 / 整理」等动词 → "actionable"；纯记录类（"下周再讨论"）→ "info"
     - suggested_command：当 kind="actionable" 时给一个可直接发到指令栏的短语，必须以 @ 开头（如 "@生成 PPT 主题"、"@查 关键词"、"@生成 Word 周报"）；info 时填 null
   - 没有任何待办时 todos 返回 []
6. 只输出 JSON，不要 markdown 围栏
"""

_MINUTES_COMPACTION_SYS_PROMPT = """你是会议事实压缩器。只提炼输入中明确出现的：
- 主题与关键事实
- 结论与决定
- 待办、负责人和时间要求

使用紧凑中文要点；保留人名、数字和专有名词；不要编造，不要输出 JSON。"""
_CONTEXT_FRAMING_RESERVE = 256
_CONTEXT_MESSAGE_RESERVE = 16
_CONTEXT_SAFETY_RESERVE = 384
_MINUTES_COMPACTION_MAX_OUTPUT_TOKENS = 768
_MINUTES_COMPACTION_MAX_ROUNDS = 4
# ``/models`` only proves the gateway catalog is reachable. The upstream may
# still reject a long ``/chat/completions`` body before the model sees it. Keep
# minutes prompts well below the observed gateway body ceiling and re-compact
# once more aggressively if a provider still returns 413.
_MINUTES_REQUEST_MAX_BYTES = 8 * 1024
_MINUTES_FALLBACK_REQUEST_MAX_BYTES = 4 * 1024
_MINUTES_REQUEST_ENVELOPE_RESERVE_BYTES = 1024
_MINUTES_MIN_COMPACTION_CHUNK_BYTES = 256


def _estimate_context_units(messages: list[ChatMessage]) -> int:
    """Mirror the gateway adapter's model-independent UTF-8 upper bound."""

    content_and_roles = sum(
        len(message.content.encode("utf-8")) + len(message.role.encode("utf-8"))
        for message in messages
    )
    return max(
        1,
        content_and_roles
        + _CONTEXT_FRAMING_RESERVE
        + len(messages) * _CONTEXT_MESSAGE_RESERVE,
    )


def _estimate_minutes_request_bytes(messages: list[ChatMessage]) -> int:
    """Conservatively bound the serialized chat body before it reaches the gateway."""

    return _MINUTES_REQUEST_ENVELOPE_RESERVE_BYTES + sum(
        len(message.role.encode("utf-8")) + len(message.content.encode("utf-8"))
        for message in messages
    )


def _minutes_final_messages(title: str, source: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=_MINUTES_SYS_PROMPT),
        ChatMessage(
            role="user",
            content=f"会议标题：{title}\n\n逐字稿：\n{source}",
        ),
    ]


def _split_utf8_chunks(text: str, max_bytes: int) -> list[str]:
    """Split on transcript lines without breaking a UTF-8 character."""

    if max_bytes < 1:
        raise ValueError("minutes chunk budget must be positive")
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_bytes
        if current:
            chunk = "".join(current)
            if chunk.strip():
                chunks.append(chunk)
        current = []
        current_bytes = 0

    for line in text.splitlines(keepends=True) or [text]:
        line_bytes = len(line.encode("utf-8"))
        if line_bytes <= max_bytes:
            if current and current_bytes + line_bytes > max_bytes:
                flush()
            current.append(line)
            current_bytes += line_bytes
            continue

        flush()
        piece: list[str] = []
        piece_bytes = 0
        for char in line:
            char_bytes = len(char.encode("utf-8"))
            if piece and piece_bytes + char_bytes > max_bytes:
                chunks.append("".join(piece))
                piece = []
                piece_bytes = 0
            piece.append(char)
            piece_bytes += char_bytes
        if piece:
            current = piece
            current_bytes = piece_bytes
    flush()
    return chunks


class MeetingPipelineError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MeetingPipeline:
    """Stateful meeting overlay for canonical recognized transcript segments."""

    def __init__(
        self,
        *,
        settings: Settings,
        rag: RagPort,
        llm: LLMPort,
        event_bus: EventBusPort | None = None,
        repository: RepositoryPort | None = None,
        stt: STTPort | None = None,
        diarizer: DiarizerPort | None = None,
    ) -> None:
        # ``stt`` / ``diarizer`` remain accepted only so downstream constructors
        # can migrate independently. MeetingPipeline no longer owns audio decode
        # or recognition; canonical capture injects recognized segments through
        # ``ingest_from_stt``.
        del stt, diarizer
        self._settings = settings
        self._rag = rag
        self._llm = llm
        self._event_bus = event_bus
        self._repo = repository

        self._segments: dict[str, list[TranscriptSegment]] = defaultdict(list)
        self._speaker_labels: dict[str, dict[str, str]] = defaultdict(dict)
        # wall-clock start（用于跨重启计算 offset_ms 与显示）
        self._started_at: dict[str, datetime] = {}
        self._wall_clock_start: dict[str, float] = {}
        self._finalized: set[str] = set()
        self._start_hydration_tasks: dict[str, asyncio.Task[None]] = {}
        self._speaker_enrichment_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._transcript_dir = scoped_directory(
            Path(settings.storage_dir).expanduser() / "meetings"
        )
        self._transcript_dir.mkdir(parents=True, exist_ok=True)

    async def hydrate_from_repo(self) -> int:
        """从 repository 恢复"未 finalized"的会议状态到内存（startup 调）。

        重启后：
        - state=in_meeting 的会议被加载，可继续 ingest / finalize
        - 新 segment 继续按采集完成时间与持久 started_at 计算偏移；模型耗时和
          进程重启不再进入会议时间轴

        注意：state=ended 的会议不 hydrate（用户已显式停了，不能再加 chunk）。
        """
        if self._repo is None:
            return 0
        meetings = await self._repo.list_meetings(state="in_meeting", limit=100)
        for m in meetings:
            segs = await self._repo.list_meeting_segments(m.id)
            labels = await self._repo.get_meeting_speaker_labels(m.id)
            async with self._lock:
                self._segments[m.id] = list(segs)
                self._speaker_labels[m.id] = dict(labels)
                self._started_at[m.id] = m.started_at
                # 仅保留活动会议的进程内 append gate；时间轴使用持久 started_at。
                self._wall_clock_start[m.id] = time.monotonic()
                self._finalized.discard(m.id)
        return len(meetings)

    async def load_meeting_for_retry(self, meeting_id: str) -> bool:
        """把已 ended（含 generation_failed）会议的 segments 重新装回内存。

        用于 ``POST /meetings/{id}/finalize`` 的重试场景：
        - 重启后 hydrate_from_repo 不会捞 state="ended" 的会议（按设计）
        - 但 minutes_status="generation_failed" 的需要被重新喂给 LLM 一次

        返回 True 表示已加载 segments（>0 条），可以接着调 ``finalize_meeting``；
        False 表示 repo 里查不到 / 没有 segments。
        """
        if self._repo is None:
            return False
        segs = await self._repo.list_meeting_segments(meeting_id)
        if not segs:
            return False
        labels = await self._repo.get_meeting_speaker_labels(meeting_id)
        rec = await self._repo.get_meeting(meeting_id)
        started_at = rec.started_at if rec else datetime.now(UTC)
        async with self._lock:
            self._segments[meeting_id] = list(segs)
            self._speaker_labels[meeting_id] = dict(labels)
            self._started_at.setdefault(meeting_id, started_at)
            self._wall_clock_start.setdefault(meeting_id, time.monotonic())
            self._finalized.discard(meeting_id)  # 允许重试
        return True

    async def _publish(self, event_type: str, meeting_id: str, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            EchoEvent(type=event_type, meeting_id=meeting_id, payload=payload)  # type: ignore[arg-type]
        )

    async def start_meeting(
        self,
        meeting_id: str,
        *,
        title: str | None = None,
        auto_started: bool = False,
        state_event_reason: str | None = None,
        started_at: datetime | None = None,
    ) -> MeetingRecord:
        """Commit a meeting and return its authoritative snapshot immediately.

        Multiple backend instances may race from an idle snapshot.  The
        repository chooses one winner; every losing pipeline hydrates and
        adopts that meeting rather than initializing a second phantom id.
        Start notifications are staged by the repository in the same durable
        transaction.  Historical hydration for a concurrently adopted row is
        deferred and gated before the first operation that needs its segments.
        """
        now = started_at or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        record = MeetingRecord(
            id=meeting_id,
            title=title,
            state="in_meeting",
            started_at=now,
            auto_started=auto_started,
        )
        needs_adopted_hydration = False
        if self._repo is not None:
            boundary_creator = getattr(self._repo, "create_meeting_boundary", None)
            if callable(boundary_creator):
                result = await boundary_creator(
                    meeting_id,
                    started_at=now,
                    title=title,
                    auto_started=auto_started,
                    state_event_reason=state_event_reason,
                )
                record = result.meeting
                created = result.created
            else:
                # Lightweight compatibility repositories do not expose the
                # insert disposition; they have no persisted history to hydrate.
                record = await self._repo.create_meeting(
                    meeting_id,
                    started_at=now,
                    title=title,
                    auto_started=auto_started,
                    state_event_reason=state_event_reason,
                )
                created = True
            needs_adopted_hydration = not created and record.id not in self._wall_clock_start
        async with self._lock:
            self._segments.setdefault(record.id, [])
            self._speaker_labels.setdefault(record.id, {})
            self._started_at.setdefault(record.id, record.started_at)
            self._wall_clock_start.setdefault(record.id, time.monotonic())
            self._finalized.discard(record.id)
        if needs_adopted_hydration:
            self._schedule_adopted_hydration(record.id)
        elif self._repo is None:
            # Pure-memory adapters have no durable outbox.  Keep their legacy
            # notification contract without affecting the canonical DB path.
            await self._publish(
                "meeting.started",
                record.id,
                {"auto_started": record.auto_started, "title": record.title},
            )
            if state_event_reason is not None:
                await self._publish(
                    "meeting.state_changed",
                    record.id,
                    {
                        "mode": "in_meeting",
                        "started_by": "auto" if record.auto_started else "manual",
                        "reason": state_event_reason,
                    },
                )
        return record

    async def start_auto_meeting_with_backfill(
        self,
        meeting_id: str,
        *,
        detected_at: datetime,
        fallback_started_at: datetime,
        state_event_reason: str,
    ) -> tuple[MeetingRecord, int]:
        """Create the automatic boundary and import its detection prehistory.

        Auto detection necessarily fires after speech has already been stored
        as ambient rows. The same current-device rows therefore define both
        the real meeting boundary and the initial transcript. Repository
        provenance keys keep this import idempotent and prevent the triggering
        capture from being appended again by the live overlay.
        """

        detected = detected_at
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=UTC)
        fallback = fallback_started_at
        if fallback.tzinfo is None:
            fallback = fallback.replace(tzinfo=UTC)

        candidates = []
        if self._repo is not None and self._settings.automeet_backfill_window_s > 0:
            since = detected - timedelta(
                seconds=self._settings.automeet_backfill_window_s
            )
            candidates = await self._repo.list_ambient_segments(
                since=since,
                until=detected,
                current_device_only=True,
                limit=200,
            )
        eligible = [item for item in candidates if item.text.strip()]
        boundary_at = fallback
        if eligible:
            candidate_starts: list[datetime] = []
            for item in eligible:
                captured_at = item.captured_at
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=UTC)
                candidate_starts.append(
                    captured_at - timedelta(milliseconds=max(0, item.duration_ms))
                )
            boundary_at = min(
                candidate_starts
            )
        record = await self.start_meeting(
            meeting_id,
            auto_started=True,
            state_event_reason=state_event_reason,
            started_at=boundary_at,
        )
        if self._repo is None or not record.auto_started or not eligible:
            return record, 0

        importer = getattr(self._repo, "import_ambient_segments_to_meeting", None)
        if not callable(importer):
            return record, 0
        import_result = await importer(
            record.id,
            ambient_segment_ids=[item.id for item in eligible],
            meeting_started_at=record.started_at,
        )
        if import_result.inserted_count:
            authoritative = await self._repo.list_meeting_segments(record.id)
            async with self._lock:
                self._segments[record.id] = list(authoritative)
            for segment in import_result.inserted_segments:
                await self._publish(
                    "meeting.segment",
                    record.id,
                    segment.model_dump(mode="json"),
                )
        logger.info(
            "auto_meeting_backfill meeting_id=%s candidates=%d inserted=%d started_at=%s",
            record.id,
            len(eligible),
            import_result.inserted_count,
            record.started_at.isoformat(),
        )
        return record, import_result.inserted_count

    def _schedule_adopted_hydration(self, meeting_id: str) -> None:
        current = self._start_hydration_tasks.get(meeting_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._hydrate_adopted_meeting(meeting_id),
            name=f"meeting-adopt-hydrate-{meeting_id}",
        )
        self._start_hydration_tasks[meeting_id] = task

        def clear(done: asyncio.Task[None]) -> None:
            error = None if done.cancelled() else done.exception()
            if error is None and self._start_hydration_tasks.get(meeting_id) is done:
                self._start_hydration_tasks.pop(meeting_id, None)
            if error is not None:
                logger.warning(
                    "adopted meeting hydration failed meeting_id=%s error_type=%s",
                    meeting_id,
                    type(error).__name__,
                )

        task.add_done_callback(clear)

    async def _hydrate_adopted_meeting(self, meeting_id: str) -> None:
        if self._repo is None:
            return
        segments = await self._repo.list_meeting_segments(meeting_id)
        labels = await self._repo.get_meeting_speaker_labels(meeting_id)
        async with self._lock:
            if not self._segments[meeting_id]:
                self._segments[meeting_id] = list(segments)
            if not self._speaker_labels[meeting_id]:
                self._speaker_labels[meeting_id] = dict(labels)

    async def _await_adopted_hydration(self, meeting_id: str) -> None:
        task = self._start_hydration_tasks.get(meeting_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            finally:
                if task.done() and self._start_hydration_tasks.get(meeting_id) is task:
                    self._start_hydration_tasks.pop(meeting_id, None)

    async def aclose(self) -> None:
        """Cancel bounded post-commit hydration during scoped-runtime eviction."""

        tasks = tuple(self._start_hydration_tasks.values())
        self._start_hydration_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        enrichment = tuple(self._speaker_enrichment_tasks)
        self._speaker_enrichment_tasks.clear()
        for task in enrichment:
            task.cancel()
        if enrichment:
            await asyncio.gather(*enrichment, return_exceptions=True)

    async def end_meeting(
        self,
        meeting_id: str,
        *,
        ended_at: datetime | None = None,
    ) -> None:
        """结束会议叠加层（不生成纪要）；ambient 主链路不受影响。"""
        if meeting_id in self._finalized:
            return
        self._finalized.add(meeting_id)
        try:
            if self._repo is not None:
                await self._repo.update_meeting_state(
                    meeting_id,
                    state="ended",
                    ended_at=ended_at or datetime.now(UTC),
                )
        except Exception:
            # durable fence 没提交时恢复内存 append gate，允许调用方重试结束。
            self._finalized.discard(meeting_id)
            raise
        await self._publish("meeting.ended", meeting_id, {})

    async def settle_capture_requests(self, meeting_id: str) -> object | None:
        """Drain accepted capture work before the durable append fence closes."""

        if self._repo is None:
            return None
        settler = getattr(self._repo, "settle_capture_requests", None)
        if not callable(settler):
            return None
        result = await settler(
            meeting_id,
            timeout_s=self._settings.meeting_capture_settle_timeout_s,
        )
        logger.info(
            "meeting_capture_settle meeting_id=%s initial_pending=%s remaining_pending=%s "
            "waited_ms=%.1f timed_out=%s",
            meeting_id,
            getattr(result, "initial_pending", "unknown"),
            getattr(result, "remaining_pending", "unknown"),
            float(getattr(result, "waited_ms", 0.0)),
            bool(getattr(result, "timed_out", False)),
        )
        return result

    async def ingest_from_stt(
        self,
        meeting_id: str,
        audio_bytes: bytes,
        stt_segs: list[TranscriptSegment],
        *,
        sample_rate: int = 16_000,
        capture_operation_key: str | None = None,
        request_fingerprint: str | None = None,
        captured_at: datetime | None = None,
        source_ambient_segment_id: int | None = None,
        initial_speaker_id: str | None = None,
        initial_speaker_label: str = "未知说话人",
    ) -> list[TranscriptSegment]:
        """Append and publish STT segments recognized by canonical capture."""
        if (capture_operation_key is None) != (request_fingerprint is None):
            raise ValueError(
                "capture operation key and request fingerprint must be provided together"
            )
        if meeting_id in self._finalized:
            raise MeetingPipelineError(
                f"meeting {meeting_id} already ended",
                code="meeting_not_active",
            )
        if not stt_segs:
            return []
        captured = captured_at or datetime.now(UTC)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        audio_duration_ms = round(len(audio_bytes) / max(1, sample_rate * 2) * 1000)
        chunk_started_at = captured - timedelta(milliseconds=audio_duration_ms)
        if meeting_id not in self._wall_clock_start:
            await self.start_meeting(meeting_id, started_at=chunk_started_at)
        await self._await_adopted_hydration(meeting_id)
        meeting_started_at = self._started_at.get(meeting_id)
        if (
            meeting_started_at is None
            or meeting_id not in self._wall_clock_start
            or meeting_id in self._finalized
        ):
            raise MeetingPipelineError(
                f"meeting {meeting_id} is no longer active",
                code="meeting_not_active",
            )

        speaker_id = initial_speaker_id
        label = (
            initial_speaker_label
            if speaker_id is None
            else await self._label_for(meeting_id, speaker_id)
        )
        if meeting_started_at.tzinfo is None:
            meeting_started_at = meeting_started_at.replace(tzinfo=UTC)
        offset_ms = max(
            0,
            round((chunk_started_at - meeting_started_at).total_seconds() * 1000),
        )
        capture_correlation = (
            f"capture-{capture_operation_key[:16]}"
            if capture_operation_key is not None
            else None
        )
        out: list[TranscriptSegment] = []
        for s in stt_segs:
            out.append(
                TranscriptSegment(
                    text=s.text,
                    start_ms=offset_ms + s.start_ms,
                    end_ms=offset_ms + s.end_ms,
                    speaker_id=speaker_id,
                    speaker_label=label,
                    capture_correlation=capture_correlation,
                )
            )

        publish_segments = True
        if self._repo is not None and capture_operation_key is not None:
            assert request_fingerprint is not None
            appended = await self._repo.append_capture_meeting_segments(
                meeting_id,
                out,
                captured_at=captured,
                capture_operation_key=capture_operation_key,
                request_fingerprint=request_fingerprint,
            )
            if not appended.accepted:
                authoritative = await self._repo.list_meeting_segments(meeting_id)
                async with self._lock:
                    self._segments[meeting_id] = list(authoritative)
                    self._finalized.add(meeting_id)
                raise MeetingPipelineError(
                    f"meeting {meeting_id} is not active",
                    code="meeting_not_active",
                )
            out = list(appended.segments)
            publish_segments = appended.inserted
            if appended.inserted:
                async with self._lock:
                    self._segments[meeting_id].extend(out)
        elif self._repo is not None and source_ambient_segment_id is not None:
            importer = getattr(self._repo, "import_ambient_segments_to_meeting", None)
            if not callable(importer):
                raise MeetingPipelineError("ambient meeting importer is unavailable")
            imported = await importer(
                meeting_id,
                ambient_segment_ids=[source_ambient_segment_id],
                meeting_started_at=meeting_started_at,
            )
            if not imported.accepted:
                authoritative = await self._repo.list_meeting_segments(meeting_id)
                async with self._lock:
                    self._segments[meeting_id] = list(authoritative)
                    self._finalized.add(meeting_id)
                raise MeetingPipelineError(
                    f"meeting {meeting_id} is not active",
                    code="meeting_not_active",
                )
            out = list(imported.segments)
            publish_segments = imported.inserted_count > 0
            if imported.inserted_count:
                async with self._lock:
                    self._segments[meeting_id].extend(out)
        else:
            async with self._lock:
                self._segments[meeting_id].extend(out)
            if self._repo is not None:
                await self._persist_active_segments(meeting_id, out, captured_at=captured)

        if publish_segments:
            for seg in out:
                payload = seg.model_dump(mode="json")
                if seg.speaker_id is None and seg.speaker_label == "未知说话人":
                    payload["speaker_status"] = "pending"
                await self._publish("meeting.segment", meeting_id, payload)
        return out

    async def enrich_capture_speaker(
        self,
        meeting_id: str,
        *,
        capture_operation_key: str | None,
        request_fingerprint: str | None,
        speaker_id: str | None,
        speaker_label: str,
        publish_event: bool = True,
    ) -> list[TranscriptSegment]:
        """Update canonical capture rows; enrichment never appends a transcript."""
        if self._repo is not None and capture_operation_key and request_fingerprint:
            updated = await self._repo.update_capture_meeting_speaker(
                capture_operation_key=capture_operation_key,
                request_fingerprint=request_fingerprint,
                speaker_id=speaker_id,
                speaker_label=speaker_label,
            )
        else:
            updated = []
            async with self._lock:
                for index, segment in enumerate(self._segments.get(meeting_id, [])):
                    if segment.speaker_label == "未知说话人":
                        replacement = segment.model_copy(
                            update={"speaker_id": speaker_id, "speaker_label": speaker_label}
                        )
                        self._segments[meeting_id][index] = replacement
                        updated.append(replacement)
        if updated:
            async with self._lock:
                for index, segment in enumerate(self._segments.get(meeting_id, [])):
                    if segment.speaker_label == "未知说话人":
                        self._segments[meeting_id][index] = segment.model_copy(
                            update={"speaker_id": speaker_id, "speaker_label": speaker_label}
                        )
            if publish_event:
                operation_key = (
                    f"capture-{capture_operation_key[:16]}"
                    if capture_operation_key
                    else None
                )
                await self._publish(
                    "meeting.speaker_updated",
                    meeting_id,
                    {
                        "capture_operation_key": operation_key,
                        "operation_key": operation_key,
                        "speaker_id": speaker_id,
                        "speaker_label": speaker_label,
                        "speaker_status": "identified" if speaker_id else "unknown",
                    },
                )
        return updated

    async def append_segment(self, meeting_id: str, seg: TranscriptSegment) -> TranscriptSegment:
        """直接附加一个已知 segment（用于 demo / 离线回放）。

        - 复用相同的说话人标签逻辑（speaker_id → 说话人N）
        - 仍触发 ``meeting.segment`` 事件，保持 UI 一致
        """
        if meeting_id not in self._wall_clock_start:
            await self.start_meeting(meeting_id)
        await self._await_adopted_hydration(meeting_id)
        label = seg.speaker_label or await self._label_for(meeting_id, seg.speaker_id)
        normalized = seg.model_copy(update={"speaker_label": label})
        async with self._lock:
            self._segments[meeting_id].append(normalized)
        if self._repo is not None:
            await self._persist_active_segments(
                meeting_id,
                [normalized],
                captured_at=datetime.now(UTC),
            )
        await self._publish("meeting.segment", meeting_id, normalized.model_dump(mode="json"))
        return normalized

    async def _persist_active_segments(
        self,
        meeting_id: str,
        segments: list[TranscriptSegment],
        *,
        captured_at: datetime,
    ) -> None:
        """Persist segments or reconcile local memory after a finalize fence."""
        if self._repo is None:
            return
        for segment in segments:
            accepted = await self._repo.append_meeting_segment(
                meeting_id,
                segment,
                captured_at=captured_at,
            )
            # Compatibility repositories historically returned None.  Only an
            # explicit False means the durable meeting has closed its append
            # gate while this pipeline was still processing a chunk.
            if accepted is not False:
                continue
            authoritative = await self._repo.list_meeting_segments(meeting_id)
            async with self._lock:
                self._segments[meeting_id] = list(authoritative)
                self._finalized.add(meeting_id)
            raise MeetingPipelineError(
                f"meeting {meeting_id} is not active",
                code="meeting_not_active",
            )

    async def _label_for(self, meeting_id: str, speaker_id: str | None) -> str:
        if speaker_id is None:
            return "未识别"
        mapping = self._speaker_labels[meeting_id]
        if speaker_id not in mapping:
            new_label = f"说话人{len(mapping) + 1}"
            mapping[speaker_id] = new_label
            if self._repo is not None:
                await self._repo.upsert_meeting_speaker_label(meeting_id, speaker_id, new_label)
        return mapping[speaker_id]

    def get_segments(self, meeting_id: str) -> list[TranscriptSegment]:
        return list(self._segments.get(meeting_id, []))

    async def finalize_meeting(
        self,
        meeting_id: str,
        *,
        title: str,
        commit: bool = True,
    ) -> MeetingMinutes:
        """会议结束 → LLM 生成纪要 → 落 DB + 发 ``minutes.ready``。

        失败语义（2026-05-28 修：之前 LLM 失败会让会议卡在 ``state=ended`` 且
        ``minutes_json=NULL``，UI 永远显示「纪要尚未生成」）：

        - LLM / JSON 解析失败 → repo 写 ``state="ended"`` + ``minutes_status="generation_failed"``
          + ``minutes_error=<msg>``；发 ``minutes.failed`` 事件；抛 ``MeetingPipelineError``
        - 无 segments → 写 ``no_content``；这是正常空终态，不提供无意义的 LLM 重试。
        - 重试（``state=finalized`` 且 ``meeting_id in _finalized``）：放行，重新跑 LLM；
          原 minutes_json 会被新结果覆盖（POST /meetings/{id}/finalize 的幂等语义）。
        """
        segs = await self._snapshot_segments_for_finalize(meeting_id)
        if not segs:
            if commit:
                await self._mark_minutes_no_content(meeting_id)
            raise MeetingPipelineError(
                f"meeting {meeting_id} has no segments",
                code="no_content",
            )

        transcript_text = self._render_transcript(segs)
        speakers = sorted({s.speaker_label for s in segs if s.speaker_label})
        duration_sec = max(1, math.ceil(segs[-1].end_ms / 1000))
        if self._repo is not None:
            record = await self._repo.get_meeting(meeting_id)
            if record is not None and record.ended_at is not None:
                started_at = record.started_at
                ended_at = record.ended_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                if ended_at.tzinfo is None:
                    ended_at = ended_at.replace(tzinfo=UTC)
                duration_sec = max(
                    1,
                    math.ceil((ended_at - started_at).total_seconds()),
                )

        try:
            minutes_payload = await self._llm_minutes(transcript_text, title)
        except Exception as e:
            # LLM / JSON / schema 任一失败：把状态置为 generation_failed，让 UI 给「重试」入口
            if commit:
                await self._mark_minutes_failed(meeting_id, str(e))
            raise

        # title 解析：LLM 返的 title 优先（语义化），失败则回退用户/系统给的 title
        # 没返或返了垃圾值（含 meeting_id / 空 / 超长）→ 回退
        llm_title = self._extract_display_title(minutes_payload.get("title"), fallback=title)
        todos = self._parse_todos(minutes_payload.get("todos", []))

        # action_items 字段保留作向后兼容：
        # - 新 prompt 返 todos → 把 todos.text 投影成 action_items（旧客户端仍能看到）
        # - 旧 prompt 只返 action_items（无 todos）→ 透传 action_items，保证旧测试通过
        legacy_action_items = minutes_payload.get("action_items", [])
        if todos:
            action_items_field: list[str] = [t.text for t in todos]
        elif isinstance(legacy_action_items, list):
            action_items_field = [str(x) for x in legacy_action_items]
        else:
            action_items_field = []

        minutes = MeetingMinutes(
            meeting_id=meeting_id,
            title=llm_title,
            duration_sec=duration_sec,
            speakers=speakers,
            summary=minutes_payload["summary"],
            sections=[MinutesSection(**s) for s in minutes_payload["sections"]],
            decisions=minutes_payload.get("decisions", []),
            todos=todos,
            action_items=action_items_field,
            created_at=datetime.now(UTC),
        )

        transcript_ref = await self._persist_transcript(meeting_id, segs, minutes)
        minutes.raw_transcript_ref = transcript_ref
        if not commit:
            return minutes

        committed_generation: int | None = None
        if self._repo is not None:
            committed_generation = await self._repo.update_meeting_state(
                meeting_id,
                state="finalized",
                title=title,  # 保留用户/系统传入的原始 title
                display_title=llm_title,  # ← migration 004 新列：语义化标题
                finalized_at=datetime.now(UTC),
                minutes_json=minutes.model_dump_json(),
                raw_transcript_ref=transcript_ref,
                minutes_status="ok",
                rag_projection_state="index_pending",
                # 显式覆盖之前可能写下的失败信息；空串而非 None 触发 SET（None 会被 SQL 跳过）
                minutes_error="",
            )
            if committed_generation is None:
                raise MeetingPipelineError(f"meeting {meeting_id} disappeared after minutes commit")
        await self.after_finalize_committed(
            meeting_id,
            minutes,
            expected_generation=committed_generation,
        )
        await self._publish("meeting.ended", meeting_id, {"duration_sec": duration_sec})
        await self._publish("minutes.ready", meeting_id, minutes.model_dump(mode="json"))
        # 主动建议前端 TTS 播一句简短的纪要 ack（前端可按 tts_enabled 决定真不真的播）
        ack_text = f"会议{llm_title}已结束，纪要已生成。{minutes.summary}"
        await self._publish(
            "tts.suggested",
            meeting_id,
            {"text": ack_text[:400], "kind": "minutes"},
        )
        return minutes

    async def _snapshot_segments_for_finalize(
        self,
        meeting_id: str,
    ) -> list[TranscriptSegment]:
        """Refresh from the durable source and establish an append cutoff."""
        if self._repo is None:
            async with self._lock:
                segments = list(self._segments.get(meeting_id, []))
                self._finalized.add(meeting_id)
            return segments

        snapshotter = getattr(self._repo, "snapshot_meeting_segments_for_finalize", None)
        if callable(snapshotter):
            segments = await snapshotter(meeting_id, ended_at=datetime.now(UTC))
        else:
            # Compatibility path for lightweight test adapters.  Production
            # repositories implement the transactional snapshot method.
            segments = await self._repo.list_meeting_segments(meeting_id)
        async with self._lock:
            # Replace instead of merge: an instance may already contain a local
            # copy of rows now returned by SQLite.  Replacement preserves DB
            # order and guarantees each segment appears exactly once.
            self._segments[meeting_id] = list(segments)
            self._finalized.add(meeting_id)
        return list(segments)

    async def after_finalize_committed(
        self,
        meeting_id: str,
        minutes: MeetingMinutes,
        *,
        expected_generation: int | None = None,
    ) -> None:
        """Update replayable in-memory/RAG projections after the SQLite commit."""

        self._finalized.add(meeting_id)
        segs = self.get_segments(meeting_id)
        transcript_text = self._render_transcript(segs)
        try:
            await self._index_minutes(
                meeting_id,
                minutes,
                transcript_text,
                expected_generation=expected_generation,
            )
            await self._set_rag_projection(
                meeting_id,
                state="indexed",
                projected_at=datetime.now(UTC),
                expected_generation=expected_generation,
            )
        except Exception as e:
            # RAG is a rebuildable projection.  The meeting/minutes transaction
            # is authoritative and recovery may re-index it later.
            import logging

            logging.getLogger("echodesk.meeting_pipeline").warning(
                "rag.ingest_meeting failed for %s: %s (minutes already committed)",
                meeting_id,
                e,
            )
            await self._set_rag_projection(
                meeting_id,
                state="index_failed",
                error=str(e),
                expected_generation=expected_generation,
            )

    async def _index_minutes(
        self,
        meeting_id: str,
        minutes: MeetingMinutes,
        transcript_text: str,
        *,
        expected_generation: int | None = None,
    ) -> None:
        rag_payload = "【纪要】\n" + minutes.summary + "\n\n【逐字稿】\n" + transcript_text
        await self._rag.ingest_meeting(
            meeting_id,
            rag_payload,
            minutes.title,
            projection_generation=expected_generation,
        )

    async def _set_rag_projection(
        self,
        meeting_id: str,
        *,
        state: str,
        error: str | None = None,
        projected_at: datetime | None = None,
        retry_backoff: bool = False,
        expected_generation: int | None = None,
    ) -> bool:
        if self._repo is None:
            return True
        setter = getattr(self._repo, "set_meeting_rag_projection", None)
        if setter is None:
            return True
        updated = await setter(
            meeting_id,
            state=state,
            error=error,
            projected_at=projected_at,
            retry_backoff=retry_backoff,
            expected_generation=expected_generation,
        )
        return bool(updated)

    async def delete_meeting_projection(
        self,
        meeting_id: str,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """Project a committed minutes clear before the request returns.

        SQLite remains authoritative. A failed text-projection deletion is recorded as
        replayable ``delete_failed`` state and the background repair loop will
        retry it; callers never silently lose the durable delete intent.
        """

        if self._repo is not None and expected_generation is not None:
            current = await self._repo.get_meeting(meeting_id)
            if (
                current is None
                or current.rag_projection_generation != expected_generation
                or current.rag_projection_state not in {"delete_pending", "delete_failed"}
            ):
                return False

        try:
            await self._rag.delete(
                f"meeting-{meeting_id}",
                projection_generation=expected_generation,
            )
        except Exception as exc:
            await self._set_rag_projection(
                meeting_id,
                state="delete_failed",
                error=str(exc),
                expected_generation=expected_generation,
            )
            return False
        return await self._set_rag_projection(
            meeting_id,
            state="deleted",
            projected_at=datetime.now(UTC),
            expected_generation=expected_generation,
        )

    async def repair_rag_projections(  # noqa: PLR0912 - meeting + legacy ambient replay
        self,
        *,
        limit: int = 100,
    ) -> tuple[int, int]:
        """Replay due meeting and ambient projection intent for one principal."""

        if self._repo is None:
            return 0, 0
        loader = getattr(self._repo, "list_meetings_needing_rag_projection", None)
        if loader is None:
            return 0, 0
        meetings = await loader(limit=limit)
        succeeded = 0
        for meeting in meetings:
            generation = meeting.rag_projection_generation
            try:
                if meeting.rag_projection_state in {"delete_pending", "delete_failed"}:
                    await self._rag.delete(
                        f"meeting-{meeting.id}",
                        projection_generation=generation,
                    )
                    projected = await self._set_rag_projection(
                        meeting.id,
                        state="deleted",
                        projected_at=datetime.now(UTC),
                        expected_generation=generation,
                    )
                else:
                    if not meeting.minutes_json:
                        raise MeetingPipelineError("minutes missing for RAG index repair")
                    minutes = MeetingMinutes.model_validate_json(meeting.minutes_json)
                    segments = await self._repo.list_meeting_segments(meeting.id)
                    await self._index_minutes(
                        meeting.id,
                        minutes,
                        self._render_transcript(segments),
                        expected_generation=generation,
                    )
                    projected = await self._set_rag_projection(
                        meeting.id,
                        state="indexed",
                        projected_at=datetime.now(UTC),
                        expected_generation=generation,
                    )
                if projected:
                    succeeded += 1
            except Exception as exc:
                operation = (
                    "delete"
                    if meeting.rag_projection_state in {"delete_pending", "delete_failed"}
                    else "index"
                )
                await self._set_rag_projection(
                    meeting.id,
                    state=f"{operation}_failed",
                    error=str(exc),
                    retry_backoff=True,
                    expected_generation=generation,
                )

        ambient_loader = getattr(
            self._repo,
            "list_ambient_segments_needing_rag_projection",
            None,
        )
        ambient_segments = await ambient_loader(limit=limit) if ambient_loader else []
        ambient_setter = getattr(self._repo, "set_ambient_rag_projection", None)
        for segment in ambient_segments:
            try:
                if segment.rag_projection_state == "reconcile_pending":
                    reconciler = getattr(self._rag, "contains_ambient_segment", None)
                    if reconciler is not None and await reconciler(
                        segment.text,
                        captured_at=segment.captured_at.isoformat(),
                        audio_ref=segment.audio_ref,
                    ):
                        if ambient_setter is not None and await ambient_setter(
                            segment.id,
                            state="indexed",
                            projected_at=datetime.now(UTC),
                        ):
                            succeeded += 1
                        continue
                await self._rag.ingest_ambient_segment(
                    segment.text,
                    captured_at=segment.captured_at.isoformat(),
                    audio_ref=segment.audio_ref,
                    speaker_id=segment.speaker_id,
                    speaker_label=segment.speaker_label,
                    operation_id=f"ambient-segment:{segment.id}",
                )
                if ambient_setter is not None and await ambient_setter(
                    segment.id,
                    state="indexed",
                    projected_at=datetime.now(UTC),
                ):
                    succeeded += 1
            except Exception as exc:
                if ambient_setter is not None:
                    await ambient_setter(
                        segment.id,
                        state="index_failed",
                        error=str(exc),
                        retry_backoff=True,
                    )
        return len(meetings) + len(ambient_segments), succeeded

    @staticmethod
    def _extract_display_title(raw: object, *, fallback: str) -> str:
        """从 LLM 返回的 title 字段提取干净的语义化标题。

        防御场景：
        - 返回 None / 非 str → 用 fallback
        - 空白 / 含 meeting_id 模式（``m-` 开头 + 12 位 hex）→ 视为无效
        - 超长 → 截到 18 字（用户需求的硬约束）
        """
        if not isinstance(raw, str):
            return fallback
        s = raw.strip()
        if not s:
            return fallback
        # m-bdd1da4e7e21 / auto-... 这类前缀视为无效
        if s.startswith(("m-", "auto-")) and len(s) <= 32:
            return fallback
        # 18 字硬上限（中文按字符数）
        if len(s) > 18:
            s = s[:18]
        return s

    @staticmethod
    def _parse_todos(raw_todos: object) -> list[TodoItem]:
        """把 LLM 返回的 todos 列表标准化成 ``list[TodoItem]``。

        宽容策略：
        - 非 list → 返 []
        - 单条非 dict / 缺 text → skip（不抛错让整个 finalize 失败）
        - id 服务端生成 uuid（LLM 不该决定 id）
        - kind 不在 {"actionable", "info"} → 默认 "info"
        - actionable 时 suggested_command 必须以 @ 开头，否则丢弃
        """
        if not isinstance(raw_todos, list):
            return []
        out: list[TodoItem] = []
        for raw in raw_todos:
            if not isinstance(raw, dict):
                continue
            text = raw.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            kind_raw = raw.get("kind")
            kind = kind_raw if kind_raw in ("actionable", "info") else "info"
            assignee = raw.get("assignee")
            if assignee is not None and not isinstance(assignee, str):
                assignee = None
            suggested = raw.get("suggested_command")
            if not (
                kind == "actionable"
                and isinstance(suggested, str)
                and suggested.strip().startswith("@")
            ):
                suggested = None
            out.append(
                TodoItem(
                    id=f"t-{uuid.uuid4().hex[:12]}",
                    text=text.strip(),
                    assignee=assignee.strip() if isinstance(assignee, str) else None,
                    kind=kind,
                    status="pending",
                    suggested_command=suggested.strip() if suggested else None,
                )
            )
        return out

    async def _mark_minutes_failed(self, meeting_id: str, error: str) -> None:
        """把纪要状态置为 ``generation_failed``，让 UI 给「重试」入口。

        - state → "ended"（哪怕之前是 "in_meeting"；用户已经主动结束/会议已断）
        - minutes_status → "generation_failed"
        - minutes_error → 一行 LLM/JSON 报错摘要（截断 500 字）
        - 发 ``minutes.failed`` 事件让前端 toast/横幅展示
        """
        if self._repo is not None:
            try:
                await self._repo.update_meeting_state(
                    meeting_id,
                    state="ended",
                    ended_at=datetime.now(UTC),
                    minutes_status="generation_failed",
                    minutes_error=error[:500] if error else "unknown error",
                )
            except Exception as e:  # pragma: no cover - repo 异常只日志
                import logging

                logging.getLogger("echodesk.meeting_pipeline").warning(
                    "mark_minutes_failed: repo update failed for %s: %s", meeting_id, e
                )
        await self._publish(
            "minutes.failed",
            meeting_id,
            {"error": error[:500] if error else "unknown error"},
        )

    async def _mark_minutes_no_content(self, meeting_id: str) -> None:
        """Persist the non-retryable terminal state for an empty transcript."""

        if self._repo is not None:
            await self._repo.update_meeting_state(
                meeting_id,
                state="ended",
                ended_at=datetime.now(UTC),
                minutes_status="no_content",
                minutes_error="",
            )
        await self._publish(
            "minutes.skipped",
            meeting_id,
            {"reason": "no_recognized_content"},
        )

    @staticmethod
    def _render_transcript(segs: list[TranscriptSegment]) -> str:
        parts: list[str] = []
        for s in segs:
            label = s.speaker_label or "未识别"
            ts = f"[{s.start_ms // 1000:02d}:{(s.start_ms // 1000) % 60:02d}]"
            parts.append(f"{ts} {label}: {s.text}")
        return "\n".join(parts)

    @staticmethod
    def _retryable_llm_error(error: BaseException) -> bool:
        status = getattr(error, "status", None)
        category = str(getattr(error, "category", "") or "").lower()
        return (
            category == "timeout"
            or status == 429
            or isinstance(status, int) and 500 <= status <= 599
        )

    async def _resolve_minutes_budget(
        self,
        *,
        transcript_chars: int,
        segment_count: int,
    ) -> tuple[int | None, str | None, int | None]:
        """读取 fresh capability，返回输出预算、模型与上下文上限。"""

        resolver = getattr(self._llm, "resolve_chat_capability", None)
        if not callable(resolver):
            if self._settings.minutes_max_tokens is None:
                return None, None, None
            return (
                calculate_minutes_max_tokens(
                    transcript_chars=transcript_chars,
                    segment_count=segment_count,
                    settings_limit=self._settings.minutes_max_tokens,
                    profile_output_limit=self._settings.minutes_max_tokens,
                ),
                None,
                None,
            )
        profile = await resolver()
        profile_limit = int(getattr(profile, "max_output_tokens"))
        budget = calculate_minutes_max_tokens(
            transcript_chars=transcript_chars,
            segment_count=segment_count,
            settings_limit=self._settings.minutes_max_tokens,
            profile_output_limit=profile_limit,
        )
        context_window = int(getattr(profile, "context_window_tokens", 0) or 0)
        return (
            budget,
            str(getattr(profile, "model")),
            context_window if context_window > 0 else None,
        )

    async def _minutes_chat_with_retry(
        self,
        messages: list[ChatMessage],
        *,
        budget_tokens: int | None,
        resolved_model: str | None,
        phase: str,
    ) -> str:
        """Run one final/compaction prompt with the existing bounded retry policy."""

        for attempt in range(1, self._settings.minutes_llm_max_attempts + 1):
            started = time.monotonic()
            try:
                resp = await self._llm.chat(
                    messages,
                    model=resolved_model,
                    max_tokens=budget_tokens,
                    temperature=0.2,
                )
            except Exception as error:
                category = str(getattr(error, "category", "unknown") or "unknown")
                status = getattr(error, "status", None)
                latency_ms = float(
                    getattr(error, "latency_ms", 0.0)
                    or (time.monotonic() - started) * 1000
                )
                retryable = self._retryable_llm_error(error)
                logger.warning(
                    "minutes_llm_attempt phase=%s resolved_model=%s category=%s status=%s "
                    "budget_tokens=%s attempt=%d latency_ms=%.1f retryable=%s",
                    phase,
                    getattr(error, "resolved_model", None)
                    or resolved_model
                    or "unknown",
                    category,
                    status if status is not None else "unknown",
                    budget_tokens if budget_tokens is not None else "unknown",
                    attempt,
                    latency_ms,
                    retryable and attempt < self._settings.minutes_llm_max_attempts,
                )
                if status == 413:
                    raise MeetingPipelineError(
                        "minutes request payload too large",
                        code="request_too_large",
                    ) from error
                if not retryable or attempt >= self._settings.minutes_llm_max_attempts:
                    raise
                delay = min(
                    self._settings.minutes_llm_retry_max_delay_s,
                    self._settings.minutes_llm_retry_base_delay_s * (2 ** (attempt - 1)),
                )
                jitter = random.uniform(
                    0.0,
                    min(
                        delay * 0.25,
                        max(0.0, self._settings.minutes_llm_retry_max_delay_s - delay),
                    ),
                )
                await asyncio.sleep(delay + jitter)
                continue

            latency_ms = float(resp.latency_ms or (time.monotonic() - started) * 1000)
            logger.info(
                "minutes_llm_attempt phase=%s resolved_model=%s category=success status=%s "
                "budget_tokens=%s attempt=%d latency_ms=%.1f retryable=false",
                phase,
                resp.model or resolved_model or "unknown",
                resp.http_status if resp.http_status is not None else "unknown",
                budget_tokens if budget_tokens is not None else "unknown",
                attempt,
                latency_ms,
            )
            return resp.content.strip()

        raise MeetingPipelineError("LLM minutes request failed")  # pragma: no cover

    @staticmethod
    def _is_minutes_request_too_large(error: BaseException) -> bool:
        return (
            getattr(error, "code", None) == "request_too_large"
            or getattr(error, "status", None) == 413
        )

    async def _compact_minutes_source(
        self,
        transcript_text: str,
        *,
        title: str,
        final_budget_tokens: int | None,
        resolved_model: str | None,
        context_window_tokens: int | None,
        request_max_bytes: int,
        force_compaction: bool = False,
    ) -> str:
        """Map-reduce when the transcript exceeds model context or gateway body limits."""

        def fits_final_request(source: str) -> bool:
            messages = _minutes_final_messages(title, source)
            context_fits = (
                context_window_tokens is None
                or final_budget_tokens is None
                or _estimate_context_units(messages) + final_budget_tokens
                <= context_window_tokens
            )
            return context_fits and _estimate_minutes_request_bytes(messages) <= request_max_bytes

        if not force_compaction and fits_final_request(transcript_text):
            return transcript_text

        map_budget = min(
            _MINUTES_COMPACTION_MAX_OUTPUT_TOKENS,
            max(256, (final_budget_tokens or 1_024) // 2),
        )
        empty_map_messages = [
            ChatMessage(role="system", content=_MINUTES_COMPACTION_SYS_PROMPT),
            ChatMessage(role="user", content="会议逐字稿片段：\n"),
        ]
        chunk_limits = [
            request_max_bytes - _estimate_minutes_request_bytes(empty_map_messages)
        ]
        if context_window_tokens is not None:
            chunk_limits.append(
                context_window_tokens
                - map_budget
                - _estimate_context_units(empty_map_messages)
                - _CONTEXT_SAFETY_RESERVE
            )
        chunk_bytes = min(chunk_limits)
        if chunk_bytes < _MINUTES_MIN_COMPACTION_CHUNK_BYTES:
            raise MeetingPipelineError("minutes request budget is too small for compaction")

        source = transcript_text
        for round_index in range(1, _MINUTES_COMPACTION_MAX_ROUNDS + 1):
            chunks = _split_utf8_chunks(source, chunk_bytes)
            summaries: list[str] = []
            for chunk_index, chunk in enumerate(chunks, start=1):
                messages = [
                    ChatMessage(role="system", content=_MINUTES_COMPACTION_SYS_PROMPT),
                    ChatMessage(
                        role="user",
                        content=(
                            f"第 {chunk_index}/{len(chunks)} 段（压缩轮次 {round_index}）：\n"
                            f"{chunk}"
                        ),
                    ),
                ]
                summary = await self._minutes_chat_with_retry(
                    messages,
                    budget_tokens=map_budget,
                    resolved_model=resolved_model,
                    phase=f"compact-{round_index}-{chunk_index}",
                )
                if not summary:
                    raise MeetingPipelineError("LLM returned empty minutes compaction")
                summaries.append(summary)

            source = "\n\n".join(
                f"片段 {index} 提炼：\n{summary}"
                for index, summary in enumerate(summaries, start=1)
            )
            if fits_final_request(source):
                logger.info(
                    "minutes_source_compacted original_bytes=%d compacted_bytes=%d "
                    "rounds=%d chunks=%d context_window=%s request_max_bytes=%d",
                    len(transcript_text.encode("utf-8")),
                    len(source.encode("utf-8")),
                    round_index,
                    len(chunks),
                    context_window_tokens if context_window_tokens is not None else "unknown",
                    request_max_bytes,
                )
                return source

        raise MeetingPipelineError("minutes source compaction exceeded request budget")

    async def _llm_minutes(self, transcript_text: str, title: str) -> dict[str, Any]:
        budget_tokens, resolved_model, context_window = await self._resolve_minutes_budget(
            transcript_chars=len(transcript_text),
            segment_count=transcript_text.count("\n") + 1,
        )
        async def generate_with_request_budget(
            request_max_bytes: int,
            *,
            force_compaction: bool = False,
        ) -> tuple[str, str]:
            minutes_source = await self._compact_minutes_source(
                transcript_text,
                title=title,
                final_budget_tokens=budget_tokens,
                resolved_model=resolved_model,
                context_window_tokens=context_window,
                request_max_bytes=request_max_bytes,
                force_compaction=force_compaction,
            )
            raw = await self._minutes_chat_with_retry(
                _minutes_final_messages(title, minutes_source),
                budget_tokens=budget_tokens,
                resolved_model=resolved_model,
                phase="final",
            )
            return minutes_source, raw

        try:
            _minutes_source, raw = await generate_with_request_budget(
                _MINUTES_REQUEST_MAX_BYTES
            )
        except Exception as error:
            if not self._is_minutes_request_too_large(error):
                raise
            logger.warning(
                "minutes gateway returned 413; recompacting source with request_max_bytes=%d",
                _MINUTES_FALLBACK_REQUEST_MAX_BYTES,
            )
            _minutes_source, raw = await generate_with_request_budget(
                _MINUTES_FALLBACK_REQUEST_MAX_BYTES,
                force_compaction=True,
            )

        if raw.startswith("```"):
            nl = raw.find("\n")
            raw = raw[nl + 1 :] if nl != -1 else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MeetingPipelineError(
                f"LLM minutes JSON parse failed at position {e.pos}"
            ) from e

        for key in ("summary", "sections"):
            if key not in data:
                raise MeetingPipelineError(f"missing key in minutes: {key}")
        # title / todos 是新加字段；旧 LLM 返回不带也允许（fallback 走外层），
        # 不要在这里 raise，避免老 prompt 测试一刀切失败。
        # 防御性：sections 内必含 heading + bullets
        try:
            for sec in data["sections"]:
                MinutesSection(**sec)
        except (ValidationError, TypeError) as e:
            raise MeetingPipelineError(f"sections schema invalid: {e!s}") from e
        return data  # type: ignore[no-any-return]

    # ── M_minutes_refactor：artifact → todo 回写 ───────────────────────
    async def attach_artifact_to_todo(
        self,
        meeting_id: str,
        todo_id: str,
        artifact_id: str,
    ) -> bool:
        """把生成好的 artifact 关联到 minutes_json.todos[todo_id]。

        - 找不到 meeting / minutes_json / todo_id → 返回 False（调用方决定是否日志）
        - 找到 → 把对应 todo status 置 "done" + done_at + artifact_id，重写整段
          minutes_json 到 repo；同时发 ``meeting.todo.completed`` 事件给前端
        - 复用现有 minutes.failed 路径：失败只警告日志，不抛错（artifact 已生成）

        rationale：todos 在 minutes_json blob 里（design choice in migration 004
        rationale），单 todo 状态变更走整段重写——并发风险存在但 P4 demo 量级
        够用；如果之后并发写明显，再切到独立 meeting_todos 表。
        """
        if self._repo is None:
            return False
        rec = await self._repo.get_meeting(meeting_id)
        if rec is None or not rec.minutes_json:
            return False
        try:
            data = json.loads(rec.minutes_json)
        except json.JSONDecodeError:
            return False
        todos = data.get("todos")
        if not isinstance(todos, list):
            return False
        hit = False
        now_iso = datetime.now(UTC).isoformat()
        for t in todos:
            if isinstance(t, dict) and t.get("id") == todo_id:
                t["status"] = "done"
                t["done_at"] = now_iso
                t["artifact_id"] = artifact_id
                hit = True
                break
        if not hit:
            return False
        await self._repo.update_meeting_state(
            meeting_id,
            state=rec.state,
            minutes_json=json.dumps(data, ensure_ascii=False),
        )
        await self._publish(
            "meeting.todo.completed",
            meeting_id,
            {"todo_id": todo_id, "artifact_id": artifact_id, "done_at": now_iso},
        )
        return True

    async def _persist_transcript(
        self,
        meeting_id: str,
        segs: list[TranscriptSegment],
        minutes: MeetingMinutes,
    ) -> str:
        path = self._transcript_dir / f"{physical_resource_id(meeting_id, kind='meeting')}.json"
        payload = {
            "meeting_id": meeting_id,
            "title": minutes.title,
            "segments": [s.model_dump() for s in segs],
            "minutes": minutes.model_dump(mode="json"),
        }

        def _write() -> None:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        await asyncio.to_thread(_write)
        return str(path)


__all__ = ["MeetingPipeline", "MeetingPipelineError"]
