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
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
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
from app.services.audio import normalize_audio_bytes

# M_minutes_refactor（2026-05-28）：把以前只返「summary/sections/decisions/
# action_items」的 prompt 升级为同时返「title（语义化标题，≤18 字中文）+ todos
# （含 assignee/kind/suggested_command）」的单 JSON。
#
# 为什么不拆成两次 LLM 调用：finalize 链路目前一次 LLM 已经要 60-180s，再加
# 一次延迟翻倍；让模型一次返完整 JSON 既省调用、也保证 title 与内容一致。
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


class MeetingPipelineError(RuntimeError):
    pass


class MeetingPipeline:
    """状态化会议 pipeline：内部维护每个 meeting_id 的 segments + speaker label 映射。"""

    def __init__(
        self,
        *,
        settings: Settings,
        stt: STTPort,
        diarizer: DiarizerPort,
        rag: RagPort,
        llm: LLMPort,
        event_bus: EventBusPort | None = None,
        repository: RepositoryPort | None = None,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._diarizer = diarizer
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
        self._lock = asyncio.Lock()
        self._transcript_dir = scoped_directory(
            Path(settings.storage_dir).expanduser() / "meetings"
        )
        self._transcript_dir.mkdir(parents=True, exist_ok=True)

    async def hydrate_from_repo(self) -> int:
        """从 repository 恢复"未 finalized"的会议状态到内存（startup 调）。

        重启后：
        - state=in_meeting 的会议被加载，可继续 add_chunk / finalize
        - wall_clock_start 用 monotonic.now() 重置（重启后的 offset_ms 从 0 开始；
          已有 segments 的 start_ms 不变，新 chunk 用相对重启的偏移叠加进 _segments）

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
                # 重启后 monotonic 重置：把 wall-clock 起点对齐到"now"，新 chunk 偏移 0 起算
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
    ) -> MeetingRecord:
        """Start a meeting and return the database-authoritative active row.

        Multiple backend instances may race from an idle snapshot.  The
        repository chooses one winner; every losing pipeline hydrates and
        adopts that meeting rather than initializing a second phantom id.
        """
        now = datetime.now(UTC)
        record = MeetingRecord(
            id=meeting_id,
            title=title,
            state="in_meeting",
            started_at=now,
            auto_started=auto_started,
        )
        persisted_segments: list[TranscriptSegment] = []
        persisted_labels: dict[str, str] = {}
        if self._repo is not None:
            record = await self._repo.create_meeting(
                meeting_id,
                started_at=now,
                title=title,
                auto_started=auto_started,
            )
            if record.id not in self._wall_clock_start:
                persisted_segments = await self._repo.list_meeting_segments(record.id)
                persisted_labels = await self._repo.get_meeting_speaker_labels(record.id)
        async with self._lock:
            self._segments.setdefault(record.id, list(persisted_segments))
            self._speaker_labels.setdefault(record.id, dict(persisted_labels))
            self._started_at.setdefault(record.id, record.started_at)
            self._wall_clock_start.setdefault(record.id, time.monotonic())
            self._finalized.discard(record.id)
        # 注意：不再 reset diarizer，避免清掉 ambient 链路累积的 speaker registry
        await self._publish(
            "meeting.started",
            record.id,
            {"auto_started": record.auto_started, "title": record.title},
        )
        return record

    async def end_meeting(self, meeting_id: str) -> None:
        """结束会议叠加层（不生成纪要）；ambient 主链路不受影响。"""
        if meeting_id in self._finalized:
            return
        self._finalized.add(meeting_id)
        if self._repo is not None:
            await self._repo.update_meeting_state(
                meeting_id, state="ended", ended_at=datetime.now(UTC)
            )
        await self._publish("meeting.ended", meeting_id, {})

    async def ingest_from_stt(
        self,
        meeting_id: str,
        audio_bytes: bytes,
        stt_segs: list[TranscriptSegment],
        *,
        sample_rate: int = 16_000,
    ) -> list[TranscriptSegment]:
        """Meeting 叠加层：复用 ambient 主链路已跑的 STT 结果，只补 diarization + WS。"""
        if meeting_id in self._finalized:
            raise MeetingPipelineError(f"meeting {meeting_id} already ended")
        if not stt_segs:
            return []
        if meeting_id not in self._wall_clock_start:
            await self.start_meeting(meeting_id)

        speaker_id = await self._diarizer.identify(audio_bytes, sample_rate=sample_rate)
        label = await self._label_for(meeting_id, speaker_id)
        offset_ms = int((time.monotonic() - self._wall_clock_start[meeting_id]) * 1000)
        captured = datetime.now(UTC)
        out: list[TranscriptSegment] = []
        async with self._lock:
            for s in stt_segs:
                seg = TranscriptSegment(
                    text=s.text,
                    start_ms=offset_ms + s.start_ms,
                    end_ms=offset_ms + s.end_ms,
                    speaker_id=speaker_id,
                    speaker_label=label,
                )
                self._segments[meeting_id].append(seg)
                out.append(seg)
        if self._repo is not None:
            await self._persist_active_segments(meeting_id, out, captured_at=captured)
        for seg in out:
            await self._publish("meeting.segment", meeting_id, seg.model_dump(mode="json"))
        return out

    async def add_audio_chunk(
        self,
        meeting_id: str,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> list[TranscriptSegment]:
        """单 chunk 入流：并发跑 STT + Diarizer。返回这段产生的 segments。"""
        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        audio_bytes = normalized.pcm
        sample_rate = normalized.sample_rate
        if meeting_id in self._finalized:
            raise MeetingPipelineError(f"meeting {meeting_id} already ended")
        if meeting_id not in self._wall_clock_start:
            await self.start_meeting(meeting_id)

        stt_task = asyncio.create_task(self._stt.transcribe(audio_bytes, sample_rate=sample_rate))
        diar_task = asyncio.create_task(
            self._diarizer.identify(audio_bytes, sample_rate=sample_rate)
        )
        try:
            segs, speaker_id = await asyncio.gather(stt_task, diar_task)
        except Exception as e:
            raise MeetingPipelineError(f"chunk pipeline failed: {e!r}") from e

        if not segs:
            return []

        label = await self._label_for(meeting_id, speaker_id)
        offset_ms = int((time.monotonic() - self._wall_clock_start[meeting_id]) * 1000)
        captured = datetime.now(UTC)
        out: list[TranscriptSegment] = []
        async with self._lock:
            for s in segs:
                seg = TranscriptSegment(
                    text=s.text,
                    start_ms=offset_ms + s.start_ms,
                    end_ms=offset_ms + s.end_ms,
                    speaker_id=speaker_id,
                    speaker_label=label,
                )
                self._segments[meeting_id].append(seg)
                out.append(seg)
        if self._repo is not None:
            await self._persist_active_segments(meeting_id, out, captured_at=captured)
        for seg in out:
            await self._publish("meeting.segment", meeting_id, seg.model_dump(mode="json"))
        return out

    async def append_segment(self, meeting_id: str, seg: TranscriptSegment) -> TranscriptSegment:
        """直接附加一个已知 segment（用于 demo / 离线回放）。

        - 复用相同的说话人标签逻辑（speaker_id → 说话人N）
        - 仍触发 ``meeting.segment`` 事件，保持 UI 一致
        """
        if meeting_id not in self._wall_clock_start:
            await self.start_meeting(meeting_id)
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
            raise MeetingPipelineError(f"meeting {meeting_id} is not active")

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
        - 无 segments → 同上（写 generation_failed）。这样 UI 始终有明确状态可展示。
        - 重试（``state=finalized`` 且 ``meeting_id in _finalized``）：放行，重新跑 LLM；
          原 minutes_json 会被新结果覆盖（POST /meetings/{id}/finalize 的幂等语义）。
        """
        segs = await self._snapshot_segments_for_finalize(meeting_id)
        if not segs:
            if commit:
                await self._mark_minutes_failed(meeting_id, "no segments to summarize")
            raise MeetingPipelineError(f"meeting {meeting_id} has no segments")

        transcript_text = self._render_transcript(segs)
        speakers = sorted({s.speaker_label for s in segs if s.speaker_label})
        duration_sec = max(1, segs[-1].end_ms // 1000)

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

        SQLite remains authoritative.  A failed BM25 deletion is recorded as
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

    @staticmethod
    def _render_transcript(segs: list[TranscriptSegment]) -> str:
        parts: list[str] = []
        for s in segs:
            label = s.speaker_label or "未识别"
            ts = f"[{s.start_ms // 1000:02d}:{(s.start_ms // 1000) % 60:02d}]"
            parts.append(f"{ts} {label}: {s.text}")
        return "\n".join(parts)

    async def _llm_minutes(self, transcript_text: str, title: str) -> dict[str, Any]:
        user_msg = f"会议标题：{title}\n\n逐字稿：\n{transcript_text}"
        resp = await self._llm.chat(
            [
                ChatMessage(role="system", content=_MINUTES_SYS_PROMPT),
                ChatMessage(role="user", content=user_msg),
            ],
            max_tokens=self._settings.minutes_max_tokens,
            temperature=0.2,
        )
        raw = resp.content.strip()
        if raw.startswith("```"):
            nl = raw.find("\n")
            raw = raw[nl + 1 :] if nl != -1 else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MeetingPipelineError(
                f"LLM minutes JSON parse failed: {e!s}; raw[:200]={raw[:200]!r}"
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
