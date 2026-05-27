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
from app.ports.repository import RepositoryPort
from app.ports.stt import STTPort
from app.schemas.events import EchoEvent
from app.schemas.llm import ChatMessage
from app.schemas.meeting import MeetingMinutes, MinutesSection, TranscriptSegment

_MINUTES_SYS_PROMPT = """你是会议纪要助手。基于以下逐字稿生成**结构化中文纪要**，严格输出 JSON：

```json
{
  "summary": "2-3 句话核心结论",
  "sections": [
    {"heading": "议题1标题", "bullets": ["要点1", "要点2"]}
  ],
  "decisions": ["明确做出的决定"],
  "action_items": ["谁 负责 什么 何时完成"]
}
```

要求：
1. 不要照抄逐字稿，提炼要点
2. 决议和行动项必须真实出现在原文，不要编造
3. sections 按议题切分，每个 ≥ 2 个 bullets
4. 只输出 JSON，不要 markdown 围栏
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
        self._transcript_dir = Path(settings.storage_dir).expanduser() / "meetings"
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
    ) -> None:
        """启动 / 续接一个会议；如果 repo 里已存在 in_meeting 行则保留旧 started_at。"""
        now = datetime.now(UTC)
        async with self._lock:
            self._segments.setdefault(meeting_id, [])
            self._speaker_labels.setdefault(meeting_id, {})
            self._started_at.setdefault(meeting_id, now)
            self._wall_clock_start.setdefault(meeting_id, time.monotonic())
            self._finalized.discard(meeting_id)
        # 注意：不再 reset diarizer，避免清掉 ambient 链路累积的 speaker registry
        if self._repo is not None:
            await self._repo.create_meeting(
                meeting_id,
                started_at=self._started_at[meeting_id],
                title=title,
                auto_started=auto_started,
            )
        await self._publish(
            "meeting.started",
            meeting_id,
            {"auto_started": auto_started, "title": title},
        )

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
            for seg in out:
                await self._repo.append_meeting_segment(meeting_id, seg, captured_at=captured)
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
            for seg in out:
                await self._repo.append_meeting_segment(meeting_id, seg, captured_at=captured)
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
            await self._repo.append_meeting_segment(
                meeting_id, normalized, captured_at=datetime.now(UTC)
            )
        await self._publish("meeting.segment", meeting_id, normalized.model_dump(mode="json"))
        return normalized

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
    ) -> MeetingMinutes:
        if meeting_id in self._finalized:
            raise MeetingPipelineError(f"meeting {meeting_id} already ended")
        segs = self.get_segments(meeting_id)
        if not segs:
            raise MeetingPipelineError(f"meeting {meeting_id} has no segments")

        transcript_text = self._render_transcript(segs)
        speakers = sorted({s.speaker_label for s in segs if s.speaker_label})
        duration_sec = max(1, segs[-1].end_ms // 1000)

        minutes_payload = await self._llm_minutes(transcript_text, title)
        minutes = MeetingMinutes(
            meeting_id=meeting_id,
            title=title,
            duration_sec=duration_sec,
            speakers=speakers,
            summary=minutes_payload["summary"],
            sections=[MinutesSection(**s) for s in minutes_payload["sections"]],
            decisions=minutes_payload.get("decisions", []),
            action_items=minutes_payload.get("action_items", []),
            created_at=datetime.now(UTC),
        )

        transcript_ref = await self._persist_transcript(meeting_id, segs, minutes)
        minutes.raw_transcript_ref = transcript_ref

        # RAG 入库（纪要 summary + 逐字稿一起检索）
        rag_payload = "【纪要】\n" + minutes.summary + "\n\n【逐字稿】\n" + transcript_text
        await self._rag.ingest_meeting(meeting_id, rag_payload, title)

        self._finalized.add(meeting_id)
        if self._repo is not None:
            await self._repo.update_meeting_state(
                meeting_id,
                state="finalized",
                title=title,
                finalized_at=datetime.now(UTC),
                minutes_json=minutes.model_dump_json(),
                raw_transcript_ref=transcript_ref,
            )
        await self._publish("meeting.ended", meeting_id, {"duration_sec": duration_sec})
        await self._publish("minutes.ready", meeting_id, minutes.model_dump(mode="json"))
        # 主动建议前端 TTS 播一句简短的纪要 ack（前端可按 tts_enabled 决定真不真的播）
        ack_text = f"会议{title}已结束，纪要已生成。{minutes.summary}"
        await self._publish(
            "tts.suggested",
            meeting_id,
            {"text": ack_text[:400], "kind": "minutes"},
        )
        return minutes

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
            max_tokens=80_000,
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
        # 防御性：sections 内必含 heading + bullets
        try:
            for sec in data["sections"]:
                MinutesSection(**sec)
        except (ValidationError, TypeError) as e:
            raise MeetingPipelineError(f"sections schema invalid: {e!s}") from e
        return data  # type: ignore[no-any-return]

    async def _persist_transcript(
        self,
        meeting_id: str,
        segs: list[TranscriptSegment],
        minutes: MeetingMinutes,
    ) -> str:
        path = self._transcript_dir / f"{meeting_id}.json"
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
