"""Ambient 主链路 UseCase：落盘 + STT + RAG；Meeting 为可选叠加层。

设计（方案 2 · 数字分身）：
- 每个 chunk **必**走 ambient（会议外音频不丢弃）
- meeting_id 可选：仅当会议 in_meeting 时叠加 MeetingPipeline（复用同一次 STT）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.adapters.audio_gate import is_likely_hallucination, pre_stt_gate
from app.config import Settings
from app.ports.diarizer import DiarizerPort
from app.ports.event_bus import EventBusPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.ports.stt import STTPort
from app.schemas.capture import CaptureChunkResult
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

logger = logging.getLogger("echodesk.ambient")


class AmbientCapturePipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        stt: STTPort,
        rag: RagPort,
        meeting: MeetingPipeline,
        repository: RepositoryPort | None = None,
        diarizer: DiarizerPort | None = None,
        speaker_registry: SpeakerRegistry | None = None,
        meeting_state: MeetingState | None = None,
        event_bus: EventBusPort | None = None,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._rag = rag
        self._meeting = meeting
        self._repo = repository
        self._diarizer = diarizer
        self._registry = speaker_registry
        self._state = meeting_state
        self._event_bus = event_bus
        self._ambient_dir = Path(settings.storage_dir).expanduser() / "ambient"
        self._ambient_dir.mkdir(parents=True, exist_ok=True)

    def _persist_wav(self, audio_bytes: bytes, sample_rate: int) -> str:
        now = datetime.now(UTC)
        day_dir = self._ambient_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        name = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.wav"
        path = day_dir / name
        path.write_bytes(audio_bytes)
        return str(path)

    async def ingest_chunk(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> CaptureChunkResult:
        audio_ref = await asyncio.to_thread(self._persist_wav, audio_bytes, sample_rate)

        captured_dt = datetime.now(UTC)
        captured_at = captured_dt.isoformat()

        # ── 前置音频门控（RMS + 帧级 VAD） ──
        # 静音/底噪 chunk 跳过 STT/diarizer（防 STT 幻觉 + speaker 编号爆炸），
        # 但仍走 detector.observe 以便正确触发自动 end（silence_timeout）。
        gate = pre_stt_gate(
            audio_bytes,
            rms_gate=self._settings.ambient_rms_gate,
            frame_rms_threshold=self._settings.ambient_frame_rms_threshold,
            min_speech_frame_ratio=self._settings.ambient_min_speech_frame_ratio,
        )

        stt_segs: list = []
        speaker_id: str | None = None
        # 串行 STT → hallu 门控 → diarize（修 ARCH-AUDIT §4 root #4）。
        # 之前是 asyncio.gather(stt, diarize)，并发能省 ~50ms 但代价是幻觉
        # chunk 上 diarizer 仍然会注册新 profile → ECAPA._profiles 累积污染。
        # echo `pipeline.py:652-678` 也是串行：先 STT，文本通过 → 再 diarize。
        if gate.pass_:
            stt_segs = await self._safe_stt(audio_bytes, sample_rate)
        else:
            logger.debug(
                "ambient gated: %s rms=%.0f ratio=%.2f",
                gate.reason, gate.rms, gate.speech_ratio,
            )

        ambient_stored = False
        ambient_text: str | None = None
        texts = [s.text.strip() for s in stt_segs if s.text.strip()]

        # ── 后置 STT 幻觉门控 ──
        hallu_drop = False
        if texts:
            joined = " ".join(texts)
            hallu, why = is_likely_hallucination(
                joined,
                audio_bytes,
                max_cps=self._settings.ambient_max_cps,
                min_chars=self._settings.ambient_min_stt_chars,
            )
            if hallu:
                logger.debug("ambient hallu drop: %s text=%r", why, joined)
                texts = []
                stt_segs = []
                hallu_drop = True

        # 仅在 STT 通过 + 非幻觉时才 diarize（避免 _profiles 被噪声/幻觉污染）
        if gate.pass_ and texts and not hallu_drop and self._diarizer is not None:
            speaker_id = await self._safe_diarize(audio_bytes, sample_rate)

        speaker_label: str | None = None
        if self._registry is not None and texts:
            speaker_label = await self._registry.label_for(speaker_id, captured_at=captured_dt)

        if texts:
            ambient_text = " ".join(texts)
            duration_ms = max(0, max((s.end_ms for s in stt_segs), default=0))
            try:
                await self._rag.ingest_ambient_segment(
                    ambient_text,
                    captured_at=captured_at,
                    audio_ref=audio_ref,
                    speaker_id=speaker_id,
                    speaker_label=speaker_label,
                )
                ambient_stored = True
            except Exception as e:
                logger.warning("ambient RAG ingest failed: %s", e)
            if self._repo is not None:
                try:
                    await self._repo.append_ambient_segment(
                        audio_ref=audio_ref,
                        text=ambient_text,
                        captured_at=captured_dt,
                        speaker_id=speaker_id,
                        speaker_label=speaker_label,
                        duration_ms=duration_ms,
                    )
                except Exception as e:
                    logger.warning("ambient repo persist failed: %s", e)

        # 自动会议检测：交给 MeetingState（单例状态机）；它内部协调 detector。
        # ambient 主链路只负责"喂观测"，状态/落库由 MeetingState 全权决定。
        effective_meeting_id: str | None = meeting_id
        if self._state is not None and meeting_id is None:
            duration_ms_obs = (
                max((s.end_ms for s in stt_segs), default=0) if stt_segs else 0
            )
            try:
                effective_meeting_id = await self._state.observe_chunk(
                    speaker_id=speaker_id,
                    duration_ms=duration_ms_obs,
                    now=captured_dt,
                )
            except Exception as e:
                logger.warning("meeting_state.observe_chunk failed: %s", e)

        meeting_segments = []
        if effective_meeting_id and texts:
            try:
                meeting_segments = await self._meeting.ingest_from_stt(
                    effective_meeting_id,
                    audio_bytes,
                    stt_segs,
                    sample_rate=sample_rate,
                )
            except MeetingPipelineError as e:
                logger.debug("meeting overlay skipped: %s", e)

        return CaptureChunkResult(
            ambient_stored=ambient_stored,
            ambient_text=ambient_text,
            audio_ref=audio_ref,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
            meeting_id=effective_meeting_id,
            meeting_segments=meeting_segments,
        )

    async def _safe_stt(
        self, audio_bytes: bytes, sample_rate: int
    ) -> list:  # type: ignore[type-arg]
        try:
            return await self._stt.transcribe(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            logger.warning("ambient STT failed (audio saved): %s", e)
            return []

    async def _safe_diarize(self, audio_bytes: bytes, sample_rate: int) -> str | None:
        """声纹识别 ambient 入口（spk-2 改为走句级切片接口）。

        改前：整段 6s chunk 一次 embed → 多人混音 / 噪声主导时被判新人。
        改后：identify_segments 在内部按 VAD 切段、每段独立 embed + EMA；本函数取
              "时长加权主导 speaker"（也即整 chunk 里说得最久的人）作为 chunk 的代表。

        若 diarizer 没实现 identify_segments（NullDiarizer 之外）则降级回老 identify。
        """
        if self._diarizer is None:
            return None
        try:
            if hasattr(self._diarizer, "identify_segments"):
                segs = await self._diarizer.identify_segments(
                    audio_bytes, sample_rate=sample_rate
                )
                if not segs:
                    return None
                # 时长加权聚合：同一 sid 累加 duration，取最长
                by_id: dict[str, int] = {}
                for s in segs:
                    sid = getattr(s, "speaker_id", None)
                    if sid is None:
                        continue
                    by_id[sid] = by_id.get(sid, 0) + int(
                        getattr(s, "end_ms", 0) - getattr(s, "start_ms", 0)
                    )
                if not by_id:
                    return None
                dominant = max(by_id.items(), key=lambda kv: kv[1])
                if len(by_id) > 1:
                    logger.debug(
                        "ambient diarize: %d voiced segs, %d distinct sids, dominant=%s",
                        len(segs), len(by_id), dominant[0],
                    )
                return dominant[0]
            return await self._diarizer.identify(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            logger.warning("ambient diarizer failed: %s", e)
            return None
