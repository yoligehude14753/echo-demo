"""Ambient 主链路 UseCase：落盘 + STT + RAG；Meeting 为可选叠加层。

设计（方案 2 · 数字分身）：
- 每个 chunk **必**走 ambient（会议外音频不丢弃）
- meeting_id 可选：仅当会议 in_meeting 时叠加 MeetingPipeline（复用同一次 STT）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.adapters.audio_gate import is_likely_hallucination, pre_stt_gate
from app.config import Settings
from app.ports.diarizer import DiarizerPort
from app.ports.event_bus import EventBusPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.ports.stt import STTPort
from app.schemas.capture import CaptureChunkResult, SttStatus
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

logger = logging.getLogger("echodesk.ambient")


# ─── M_diag_brake：7 道门诊断 ────────────────────────────────────────────
#
# 用户事故复盘：8 小时 4495 个 ambient chunk → 0 段入库。后端日志 198 条
# `ambient STT failed`，其中 122 条是 `firered circuit open`。用户必须翻日志
# 才能定位「哪道门把声音吃了」。本 dataclass 把整条链路的处理结果累加成进程
# 级 in-memory 计数器，配合 GET /capture/stats 暴露给前端实时展示。
#
# 进程级 / 重启清零：当前不持久化（v1 简化）。如果未来需要跨重启留痕，应该
# 持久化到 SQLite 单独的 `ambient_pipeline_counters` 表，重启时 hydrate。


@dataclass(slots=True)
class AmbientStats:
    """ambient pipeline 处理结果计数（in-memory, 进程级）。重启清零。

    每个 chunk 进入 ingest_chunk 时 `chunks_total += 1`，并且**且仅有一个**
    末态计数器 +1（gated_rms / gated_low_speech / stt_circuit_open /
    stt_failed / stt_empty / hallu_dropped / stored）。diarize_failed 是
    side-channel：失败时 +1，但不阻断后续路径，所以可能与 stored 同时 +1。
    """

    chunks_total: int = 0  # POST 进入的 chunk 数（含所有末态）
    gated_rms: int = 0  # Gate 1a: 整段 RMS < ambient_rms_gate
    gated_low_speech: int = 0  # Gate 1b: 帧级活跃率 < min_speech_frame_ratio
    stt_circuit_open: int = 0  # Gate 2a: STT 熔断（未发起请求）
    stt_failed: int = 0  # Gate 2b: STT 发了但失败（超时/网络/5xx）
    stt_empty: int = 0  # Gate 3:  STT 返回空文本 / 所有 segs 文本为空
    hallu_dropped: int = 0  # Gate 4:  后置幻觉门丢弃（cps 过高 / 过短）
    diarize_failed: int = 0  # side: diarizer 失败（不影响入库）
    stored: int = 0  # 末态: 真正写入 ambient_segments 表
    last_chunk_at: str | None = None  # ISO timestamp 最近 chunk 进入时间
    last_stored_at: str | None = None  # ISO timestamp 最近一次成功入库时间


class _STTCircuitOpenError(RuntimeError):
    """`_safe_stt` 内部信号：STTPort 抛出 `STTError("...circuit open...")`。

    与普通 STT 失败区分开，让 `ingest_chunk` 能把对应 chunk 标记成
    `stt_status="circuit_open"`，触发前端优雅止血。
    """


class _STTCallFailedError(RuntimeError):
    """`_safe_stt` 内部信号：STT 调用本身失败（超时、网络、5xx 等）。

    与熔断区分开是因为：熔断 → 前端应停止上传（reactive backoff）；
    单次失败 → 前端继续上传（下一 chunk 可能成功）。
    """


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
        self._stats = AmbientStats()

    def get_stats(self) -> AmbientStats:
        """返回当前进程级 7 道门处理结果计数（供 GET /capture/stats 用）。"""
        return self._stats

    def _persist_wav(self, audio_bytes: bytes, sample_rate: int) -> str:
        now = datetime.now(UTC)
        day_dir = self._ambient_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        name = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.wav"
        path = day_dir / name
        path.write_bytes(audio_bytes)
        return str(path)

    async def ingest_chunk(  # noqa: PLR0912, PLR0915
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> CaptureChunkResult:
        audio_ref = await asyncio.to_thread(self._persist_wav, audio_bytes, sample_rate)

        captured_dt = datetime.now(UTC)
        captured_at = captured_dt.isoformat()
        # M_diag_brake：每条 ingest_chunk 头部记一次（含所有末态），
        # 后端日志即使只看 chunks_total 也能粗略知道 firehose 多大。
        self._stats.chunks_total += 1
        self._stats.last_chunk_at = captured_at

        # ── 前置音频门控（RMS + 帧级 VAD） ──
        # 静音/底噪 chunk 跳过 STT/diarizer（防 STT 幻觉 + speaker 编号爆炸），
        # 但仍走 detector.observe 以便正确触发自动 end（silence_timeout）。
        gate = pre_stt_gate(
            audio_bytes,
            rms_gate=self._settings.ambient_rms_gate,
            frame_rms_threshold=self._settings.ambient_frame_rms_threshold,
            min_speech_frame_ratio=self._settings.ambient_min_speech_frame_ratio,
        )

        stt_segs: list[TranscriptSegment] = []
        speaker_id: str | None = None
        # M_diag_brake：默认 ok，后续每个分支按需覆写。
        stt_status: SttStatus = "ok"
        # 串行 STT → hallu 门控 → diarize（修 ARCH-AUDIT §4 root #4）。
        # 之前是 asyncio.gather(stt, diarize)，并发能省 ~50ms 但代价是幻觉
        # chunk 上 diarizer 仍然会注册新 profile → ECAPA._profiles 累积污染。
        # echo `pipeline.py:652-678` 也是串行：先 STT，文本通过 → 再 diarize。
        if gate.pass_:
            try:
                stt_segs = await self._safe_stt(audio_bytes, sample_rate)
            except _STTCircuitOpenError:
                # firered 已熔断；不再发起请求 → 前端应进入指数退避
                self._stats.stt_circuit_open += 1
                stt_status = "circuit_open"
                stt_segs = []
            except _STTCallFailedError:
                # 单次失败 → 前端可继续上传，但本 chunk 不入库
                self._stats.stt_failed += 1
                stt_status = "failed"
                stt_segs = []
        else:
            # Gate 1：前置音频门控拒了。区分 RMS / 帧级活跃率两条路径。
            # audio_gate.pre_stt_gate 的 reason 是 "rms_too_low" /
            # "speech_ratio_too_low"（不是用户 brief 里写的 "low_speech_ratio"）。
            if gate.reason == "rms_too_low":
                self._stats.gated_rms += 1
            elif gate.reason == "speech_ratio_too_low":
                self._stats.gated_low_speech += 1
            else:
                # 防御性：未来 audio_gate 加新 reason 时也归到帧级活跃率桶里
                # （比 silently 丢掉好，至少计数总和等于 chunks_total）
                self._stats.gated_low_speech += 1
            stt_status = "gated"
            logger.debug(
                "ambient gated: %s rms=%.0f ratio=%.2f",
                gate.reason,
                gate.rms,
                gate.speech_ratio,
            )

        ambient_stored = False
        ambient_text: str | None = None
        texts = [s.text.strip() for s in stt_segs if s.text.strip()]

        # Gate 3：STT 调用成功但返回空文本（音频里 ASR 没"听到"任何字）
        if gate.pass_ and stt_status == "ok" and not texts:
            self._stats.stt_empty += 1
            stt_status = "empty"

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
                # Gate 4：幻觉门吃掉。stt_status 不改回 "empty"——保留 "ok"
                # 语义（"STT 调用成功且有内容，只是被下游过滤了"）让前端能区分
                # "STT 健康但被过滤" vs "STT 没听到"。计数器单独记。
                self._stats.hallu_dropped += 1

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
            if ambient_stored:
                # 末态：唯一计入 stored 的位置。RAG ingest 失败时按"末态不计"处理
                # （这条 chunk 就当被 RAG/repo 吃了），避免 stored 与实际表行不一致。
                self._stats.stored += 1
                self._stats.last_stored_at = captured_at

        # 自动会议检测：交给 MeetingState（单例状态机）；它内部协调 detector。
        # ambient 主链路只负责"喂观测"，状态/落库由 MeetingState 全权决定。
        effective_meeting_id: str | None = meeting_id
        if self._state is not None and meeting_id is None:
            duration_ms_obs = max((s.end_ms for s in stt_segs), default=0) if stt_segs else 0
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
            stt_status=stt_status,
        )

    async def _safe_stt(self, audio_bytes: bytes, sample_rate: int) -> list:  # type: ignore[type-arg]
        """STT 调用 + typed exception 分流（M_diag_brake）。

        调用方需要区分"熔断（前端必须停止上传）"和"单次失败（前端可继续）"，
        所以这里把底层 Exception 翻译成两类自定义异常。之前的实现把所有错误
        都吞成 `return []`，调用方完全没法区分。

        熔断识别：firered adapter 在熔断时抛 `STTError("...circuit open...")`
        （见 backend/app/adapters/stt/firered.py:61）。本函数只看 message
        是否含 "circuit open" 子串，避免 import 具体 adapter 类（保持 Port
        独立性，未来换 adapter 也能继续 work，只要 message 约定不变）。
        """
        try:
            return await self._stt.transcribe(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            msg = str(e)
            if "circuit open" in msg.lower():
                logger.warning("ambient STT circuit open (audio saved): %s", e)
                raise _STTCircuitOpenError(msg) from e
            logger.warning("ambient STT failed (audio saved): %s", e)
            raise _STTCallFailedError(msg) from e

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
                segs = await self._diarizer.identify_segments(audio_bytes, sample_rate=sample_rate)
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
                        len(segs),
                        len(by_id),
                        dominant[0],
                    )
                return dominant[0]
            return await self._diarizer.identify(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            self._stats.diarize_failed += 1
            logger.warning("ambient diarizer failed: %s", e)
            return None
