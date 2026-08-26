"""Ambient 主链路 UseCase：落盘 + STT + RAG；Meeting 为可选叠加层。

设计（方案 2 · 数字分身）：
- 每个 chunk 必走质量门；只有有效语音持久化 WAV，静音/底噪零文件
- 有效语音即使 STT 暂时失败也保留音频，便于后续恢复/审计
- meeting_id 可选：仅当会议 in_meeting 时叠加 MeetingPipeline（复用同一次 STT）
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import stat
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from app.adapters.audio_gate import (
    is_likely_hallucination,
    normalize_transcript_text,
    pre_stt_gate,
    split_into_voiced_segments,
)
from app.config import Settings
from app.memory import MemoryScope, MemoryService
from app.memory.models import RecallCandidate
from app.memory.presentation import recall_sources
from app.ports.asr import ASRErrorBase, ASRRequestContext, ASRSchedulerPort, ASRTelemetryPort
from app.ports.diarizer import DiarizerPort
from app.ports.event_bus import EventBusPort
from app.ports.punctuator import TextPunctuatorPort
from app.ports.rag import RagPort
from app.ports.repository import AmbientAudioFileRecord, MeetingRecord, RepositoryPort
from app.ports.stt import STTPort, TranscriptStreamEvent, TranscriptStreamHandler
from app.schemas.capture import CaptureChunkResult, SttStatus
from app.schemas.events import EchoEvent
from app.schemas.meeting import TranscriptSegment
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor, QuotaExceeded, QuotaReservation
from app.security.models import Principal
from app.security.scope import scoped_directory
from app.services.audio import normalize_audio_bytes, pcm_to_wav
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

logger = logging.getLogger("echodesk.ambient")

_ADMISSION_FRAME_SAMPLES = 16_000 * 20 // 1_000
_RECENT_STRONG_SPEECH_WINDOW_S = 15.0
_STABLE_GATE_REASONS = frozenset(
    {"ok", "rms_too_low", "speech_ratio_too_low", "stationary_noise", "unknown"},
)


class AmbientPersistenceError(RuntimeError):
    """Authoritative ambient DB append failed after a non-empty transcript."""


class _AdmissionCommitLane:
    """仅保存序号与等待器的单 scope 后置副作用提交通道。"""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._assigned = 0
        self._next = 0
        self._skipped: set[int] = set()
        self._participants = 0

    async def reserve(self) -> int:
        async with self._condition:
            ordinal = self._assigned
            self._assigned += 1
            self._participants += 1
            return ordinal

    async def enter(self, ordinal: int) -> None:
        async with self._condition:
            while ordinal != self._next:
                await self._condition.wait()

    async def finish(self, ordinal: int) -> bool:
        async with self._condition:
            self._participants -= 1
            if ordinal == self._next:
                self._next += 1
                while self._next in self._skipped:
                    self._skipped.remove(self._next)
                    self._next += 1
            elif ordinal > self._next:
                self._skipped.add(ordinal)
            self._condition.notify_all()
            return self._participants == 0


@dataclass(slots=True)
class _AdmissionHandle:
    scope_hash: str
    lane: _AdmissionCommitLane
    ordinal: int
    entered: bool = False
    storage_reservation: QuotaReservation | None = None


_CURRENT_ADMISSION: ContextVar[_AdmissionHandle | None] = ContextVar(
    "ambient_capture_admission",
    default=None,
)


def _ordered_capture_admission(
    func: Callable[..., Awaitable[CaptureChunkResult]],
) -> Callable[..., Awaitable[CaptureChunkResult]]:
    """为整次 ingest 设置 finally 受保护的、无 payload 提交序号。"""

    @wraps(func)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> CaptureChunkResult:
        handle = await self._reserve_admission()
        context_token = _CURRENT_ADMISSION.set(handle)
        try:
            return await func(self, *args, **kwargs)
        finally:
            _CURRENT_ADMISSION.reset(context_token)
            await self._finish_admission(handle)

    return wrapped


def _complete_admission_frames(audio_bytes: bytes) -> int:
    """Return complete 20ms/16kHz/int16 frames in the normalized PCM buffer."""
    if _ADMISSION_FRAME_SAMPLES <= 0:
        return 0
    return max(0, len(audio_bytes) // 2 // _ADMISSION_FRAME_SAMPLES)


def _stable_gate_reason(reason: str | None) -> str:
    """Keep frontend admission labels on a small, additive allowlist."""
    if reason in _STABLE_GATE_REASONS:
        return reason
    return "unknown"


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

    每个正常返回的 chunk 有一个转写末态（gated / STT failure / empty /
    hallucination / stored / segment_store_failed）；storage quota 拒绝会在落盘前
    抛出并由 HTTP 层返回 429。audio_* 与 diarize_* 是独立 lifecycle/side-channel
    counters，可能与任一转写末态同时增加。
    """

    chunks_total: int = 0  # POST 进入的 chunk 数（含所有末态）
    gated_rms: int = 0  # Gate 1a: 整段 RMS < ambient_rms_gate
    gated_low_speech: int = 0  # Gate 1b: 帧级活跃率 < min_speech_frame_ratio
    gated_stationary_noise: int = 0  # Gate 1c: AGC 后稳定底噪，不具备语音能量起伏
    stt_circuit_open: int = 0  # Gate 2a: STT 熔断（未发起请求）
    stt_failed: int = 0  # Gate 2b: STT 发了但失败（超时/网络/5xx）
    stt_empty: int = 0  # Gate 3:  STT 返回空文本 / 所有 segs 文本为空
    hallu_dropped: int = 0  # Gate 4:  后置幻觉门丢弃（cps 过高 / 过短）
    repeat_dropped: int = 0  # Gate 4b: 短窗口内同一文本超过允许次数
    diarize_failed: int = 0  # side: diarizer 抛异常（不影响入库；与 returned_none 区分）
    # side: diarizer 正常返回 None（短段没匹配 / 全静音切不出 voiced）。phase4-diar-deep
    # 引入，区分 "diarizer 跑了但说不出是谁"（None）和 "diarizer 挂了"（failed）；
    # 用户痛点 2026-05-28 看到 57 段 NULL，过去全归类成神秘黑盒。
    diarize_returned_none: int = 0
    stored: int = 0  # 末态: 真正写入 ambient_segments 表
    segment_store_failed: int = 0  # 有有效文本，但 authoritative segment store 失败
    audio_files_stored: int = 0  # 通过质量门后成功原子落盘的 WAV 数
    audio_bytes_stored: int = 0  # 本进程成功落盘的真实 WAV 字节
    audio_store_failed: int = 0  # 编码/预留/写盘/registry 任一步失败
    audio_quota_rejected: int = 0  # public quota 或 owner cap 拒绝，且未产生文件
    audio_files_deleted: int = 0  # retention/capacity GC 实际删除的 WAV 数
    audio_bytes_deleted: int = 0  # retention/capacity GC 实际删除的字节
    audio_gc_failed: int = 0  # inventory/scan 阶段失败；本轮 fail-closed 不删除
    audio_delete_failed: int = 0  # 越界路径、unlink、registry 或 quota release 失败
    audio_missing_reconciled: int = 0  # registry 有记录但文件已不存在
    last_chunk_at: str | None = None  # ISO timestamp 最近 chunk 进入时间
    last_stored_at: str | None = None  # ISO timestamp 最近一次成功入库时间
    last_audio_stored_at: str | None = None  # ISO timestamp 最近一次 WAV 成功落盘
    last_rms: float = 0.0  # 最近 chunk 的整段 int16 RMS
    last_speech_ratio: float = 0.0  # 最近 chunk 的 20ms 活跃帧比例
    last_gate_reason: str | None = None  # 最近 chunk 的前置门控结果（ok/rms_too_low/...）
    # Process-lifetime in-memory admission window; construction/restart resets it.
    # The denominator is complete 20ms frames from every normalized chunk; the
    # numerator is active frames from chunks that passed the pre-STT gate.
    observed_audio_frames: int = 0
    accepted_speech_frames: int = 0
    accepted_speech_ratio: float = 0.0  # zero denominator is explicitly 0.0
    stats_sequence: int = 0  # increments once for every normalized ingest
    # 仅保存进程级聚合压力值；不保存请求时间、身份、音频、文本或 payload。
    request_total: int = 0
    request_success: int = 0
    request_failure: int = 0
    request_inflight: int = 0
    request_inflight_max: int = 0
    request_service_ms_sum: int = 0
    request_service_ms_max: int = 0


class _STTCircuitOpenError(RuntimeError):
    """`_safe_stt` 内部信号：STTPort 抛出 legacy `"...circuit open..."`。

    与普通 STT 失败区分开，让 `ingest_chunk` 能把对应 chunk 标记成
    `stt_status="circuit_open"`，触发前端优雅止血。
    """


class _STTCallFailedError(RuntimeError):
    """`_safe_stt` 内部信号：STT 调用本身失败（超时、网络、5xx 等）。

    与熔断区分开是因为：熔断 → 前端应停止上传（reactive backoff）；
    单次失败 → 前端继续上传（下一 chunk 可能成功）。
    """


@dataclass(frozen=True, slots=True)
class _OwnerAudioFile:
    path: Path
    size_bytes: int
    mtime: float


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
        punctuator: TextPunctuatorPort | None = None,
        asr_scheduler: ASRSchedulerPort | None = None,
        telemetry: ASRTelemetryPort | None = None,
        governor: PrincipalGovernor | None = None,
        principal: Principal | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._asr_scheduler = asr_scheduler
        self._telemetry = telemetry
        self._rag = rag
        self._meeting = meeting
        self._repo = repository
        self._diarizer = diarizer
        self._registry = speaker_registry
        self._state = meeting_state
        self._event_bus = event_bus
        self._punctuator = punctuator
        self._principal = principal or current_principal()
        self._governor = governor
        self._memory = memory
        self._memory_scope = MemoryScope.from_principal(self._principal)
        self._memory_tasks: set[asyncio.Task[None]] = set()
        self._rag_projection_tasks: set[asyncio.Task[None]] = set()
        self._speaker_enrichment_tasks: set[asyncio.Task[None]] = set()
        self._last_memory_association_at = 0.0
        self._ambient_dir = scoped_directory(
            Path(settings.storage_dir).expanduser() / "ambient",
            self._principal,
        )
        self._audio_registry_enabled = repository is not None and all(
            callable(getattr(type(repository), method, None))
            for method in (
                "register_ambient_audio_file",
                "list_ambient_audio_files",
                "delete_ambient_audio_file",
            )
        )
        self._stats = AmbientStats()
        self._stt_lock = asyncio.Lock()
        self._storage_lock = asyncio.Lock()
        self._admission_lanes: dict[str, _AdmissionCommitLane] = {}
        self._admission_lanes_lock = asyncio.Lock()
        self._recent_transcripts: deque[tuple[datetime, str]] = deque()
        self._last_strong_speech_at: datetime | None = None

    async def aclose(self) -> None:
        projections = tuple(self._rag_projection_tasks)
        self._rag_projection_tasks.clear()
        for task in projections:
            task.cancel()
        if projections:
            await asyncio.gather(*projections, return_exceptions=True)

        tasks = tuple(self._speaker_enrichment_tasks)
        self._speaker_enrichment_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_stats(self) -> AmbientStats:
        """返回当前进程级 7 道门处理结果计数（供 GET /capture/stats 用）。"""
        return self._stats

    def begin_request(self) -> float:
        """记录无敏感内容的服务尝试，并返回单次耗时的单调时钟起点。"""
        self._stats.request_total += 1
        self._stats.request_inflight += 1
        self._stats.request_inflight_max = max(
            self._stats.request_inflight_max,
            self._stats.request_inflight,
        )
        return time.monotonic()

    def finish_request(self, started_at: float, *, success: bool) -> None:
        """在成功、失败或取消时释放聚合中的服务槽位。"""
        elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
        self._stats.request_inflight = max(0, self._stats.request_inflight - 1)
        self._stats.request_service_ms_sum += elapsed_ms
        self._stats.request_service_ms_max = max(
            self._stats.request_service_ms_max,
            elapsed_ms,
        )
        if success:
            self._stats.request_success += 1
        else:
            self._stats.request_failure += 1

    async def _reserve_admission(self) -> _AdmissionHandle:
        """为认证 scope 分配只含哈希键与 ordinal 的进程内 admission。"""
        principal = self._principal
        digest = hashlib.blake2s(digest_size=16)
        digest.update(
            "\0".join((principal.tenant_id, principal.owner_id, principal.device_id)).encode()
        )
        scope_hash = digest.hexdigest()
        async with self._admission_lanes_lock:
            lane = self._admission_lanes.setdefault(scope_hash, _AdmissionCommitLane())
            ordinal = await lane.reserve()
        return _AdmissionHandle(scope_hash=scope_hash, lane=lane, ordinal=ordinal)

    async def _enter_admission_commit(self) -> None:
        """仅在 ASR 完成后开始本次 ordinal 的有序副作用提交。"""
        handle = _CURRENT_ADMISSION.get()
        if handle is None or handle.entered:
            return
        await handle.lane.enter(handle.ordinal)
        handle.entered = True

    async def _finish_admission(self, handle: _AdmissionHandle) -> None:
        """无论成功、失败或取消均推进/跳过 ordinal，避免队首阻塞。"""
        if handle.storage_reservation is not None:
            await handle.storage_reservation.release()
            handle.storage_reservation = None
        if not await handle.lane.finish(handle.ordinal):
            return
        async with self._admission_lanes_lock:
            if self._admission_lanes.get(handle.scope_hash) is handle.lane:
                self._admission_lanes.pop(handle.scope_hash, None)

    def _schedule_recognized_memory(
        self,
        *,
        text: str,
        segment_id: int,
        captured_at: datetime,
        meeting_id: str | None,
    ) -> None:
        if self._memory is None or not self._settings.memory_recognized_text_enabled:
            return
        source_id = str(segment_id)
        self._memory.schedule_extraction(
            self._memory_scope,
            text=text,
            source_kind="ambient_segment",
            source_id=source_id,
            occurred_at=captured_at,
            meeting_id=meeting_id,
            metadata={"capture": "ambient"},
        )
        if self._event_bus is None:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_memory_association_at < self._settings.memory_proactive_cooldown_s:
            return
        self._last_memory_association_at = now
        task = asyncio.create_task(
            self._publish_memory_association(text, segment_id, meeting_id),
            name=f"memory-associate:ambient:{segment_id}",
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_task_done)

    def _memory_task_done(self, task: asyncio.Task[None]) -> None:
        self._memory_tasks.discard(task)
        if task.cancelled():
            return
        if error := task.exception():
            logger.warning("recognized-text memory association failed: %s", error)

    async def _publish_memory_association(
        self,
        text: str,
        segment_id: int,
        meeting_id: str | None,
    ) -> None:
        if self._memory is None or self._event_bus is None:
            return
        result = await self._memory.recall(
            self._memory_scope,
            text,
            conversation_id=f"meeting:{meeting_id}" if meeting_id else "ambient",
            llm_priority="background",
        )
        matches = [
            match
            for match in result.matches
            if match.candidate.source_ref != f"ambient:{segment_id}"
            and not self._is_current_text_alias(match.candidate, text)
            and match.score >= self._settings.memory_proactive_min_score
        ]
        if not matches:
            return
        sources = recall_sources(result.model_copy(update={"matches": matches}))
        model_identity = (
            {
                "model_id": result.small_model,
                "model_display_name": result.small_model,
            }
            if result.small_model
            else {}
        )
        await self._event_bus.publish(
            EchoEvent(
                type="memory.sources",
                meeting_id=meeting_id,
                tenant_id=self._principal.tenant_id,
                owner_id=self._principal.owner_id,
                payload={
                    "type": "memory.sources",
                    "state": "found",
                    "label": f"识别到 {len(sources)} 条相关信息",
                    "message_id": f"ambient:{segment_id}",
                    "sources": sources,
                    **model_identity,
                },
            )
        )

    @staticmethod
    def _is_current_text_alias(
        candidate: RecallCandidate,
        text: str,
    ) -> bool:
        if candidate.level != "L0" or candidate.kind != "current_meeting":
            return False
        _speaker, separator, candidate_text = candidate.content.partition("：")
        return bool(separator) and candidate_text.strip() == text.strip()

    def _schedule_rag_projection(
        self,
        *,
        segment_id: int,
        text: str,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None,
        speaker_label: str | None,
    ) -> None:
        """Project a durable canonical row without extending capture admission."""

        if self._repo is None:
            raise RuntimeError("durable RAG projection requires a repository")
        task = asyncio.create_task(
            self._project_ambient_rag(
                repository=self._repo,
                segment_id=segment_id,
                text=text,
                captured_at=captured_at,
                audio_ref=audio_ref,
                speaker_id=speaker_id,
                speaker_label=speaker_label,
            ),
            name=f"ambient-rag-project:{segment_id}",
        )
        self._rag_projection_tasks.add(task)
        task.add_done_callback(self._rag_projection_task_done)

    def _rag_projection_task_done(self, task: asyncio.Task[None]) -> None:
        self._rag_projection_tasks.discard(task)
        if task.cancelled():
            return
        if error := task.exception():
            logger.warning(
                "ambient RAG background task failed: %s",
                type(error).__name__,
            )

    async def _project_ambient_rag(
        self,
        *,
        repository: RepositoryPort,
        segment_id: int,
        text: str,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None,
        speaker_label: str | None,
    ) -> None:
        try:
            await self._rag.ingest_ambient_segment(
                text,
                captured_at=captured_at,
                audio_ref=audio_ref,
                speaker_id=speaker_id,
                speaker_label=speaker_label,
                operation_id=f"ambient-segment:{segment_id}",
            )
        except asyncio.CancelledError:
            # The durable row remains index_pending and the lifespan repair loop
            # can safely replay the stable operation id after eviction/restart.
            raise
        except Exception as exc:
            logger.warning("ambient RAG ingest failed: %s", exc)
            try:
                await repository.set_ambient_rag_projection(
                    segment_id,
                    state="index_failed",
                    error=str(exc),
                    retry_backoff=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as projection_exc:
                logger.warning(
                    "ambient RAG failure state persist failed: %s",
                    projection_exc,
                )
            return

        try:
            await repository.set_ambient_rag_projection(
                segment_id,
                state="indexed",
                projected_at=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception as projection_exc:
            # The row remains index_pending. Repair replays the same operation id.
            logger.warning(
                "ambient RAG success state persist failed: %s",
                projection_exc,
            )

    def _record_transcript_repeat(
        self,
        text: str,
        *,
        captured_at: datetime,
    ) -> tuple[bool, bool, int]:
        """记录规范化文本，返回（是否重复、是否丢弃、窗口内既有次数）。"""

        signature = normalize_transcript_text(text)
        if not signature:
            return False, False, 0
        cutoff = captured_at.timestamp() - self._settings.ambient_repeat_window_s
        while self._recent_transcripts and self._recent_transcripts[0][0].timestamp() < cutoff:
            self._recent_transcripts.popleft()
        occurrences = sum(previous == signature for _, previous in self._recent_transcripts)
        self._recent_transcripts.append((captured_at, signature))
        return (
            occurrences >= 1,
            occurrences >= self._settings.ambient_repeat_drop_after,
            occurrences,
        )

    def _transcript_stream_handler(
        self,
        *,
        meeting_id: str | None,
        capture_operation_key: str | None,
    ) -> TranscriptStreamHandler | None:
        if self._event_bus is None or not capture_operation_key:
            return None
        correlation = f"capture-{capture_operation_key[:16]}"
        scope = (self._principal.tenant_id, self._principal.owner_id)

        async def publish(event: TranscriptStreamEvent) -> None:
            await self._event_bus.publish_to(
                scope,
                EchoEvent(
                    type="transcript.partial",
                    meeting_id=meeting_id,
                    payload={
                        "correlation": correlation,
                        "text": event.text,
                        "state": event.state,
                    },
                ),
            )

        return publish

    def _owner_root(self, *, create: bool) -> Path:
        """Return a real owner root, rejecting a scope path replaced by a symlink."""

        declared = self._ambient_dir.absolute()
        if declared.is_symlink():
            raise RuntimeError("ambient owner root must not be a symlink")
        if create:
            declared.mkdir(parents=True, exist_ok=True)
        if not declared.exists():
            return declared
        if not declared.is_dir() or declared.is_symlink():
            raise RuntimeError("ambient owner root is not a safe directory")
        return declared.resolve(strict=True)

    def _safe_owner_reference(self, path: Path | str, *, require_file: bool) -> Path | None:
        """Resolve one server-authored ref without ever escaping this owner scope."""

        candidate = Path(path)
        if not candidate.is_absolute() or candidate.is_symlink():
            return None
        try:
            root = self._owner_root(create=False).resolve(strict=False)
            resolved = candidate.resolve(strict=require_file)
            resolved.relative_to(root)
            if require_file:
                mode = candidate.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    return None
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return None
        return resolved

    def _scan_owner_wavs(self) -> tuple[list[_OwnerAudioFile], int]:
        root = self._owner_root(create=False)
        if not root.exists():
            return [], 0
        files: list[_OwnerAudioFile] = []
        unsafe = 0
        for candidate in root.rglob("*.wav"):
            resolved = self._safe_owner_reference(candidate, require_file=True)
            if resolved is None:
                unsafe += 1
                continue
            try:
                file_stat = resolved.stat()
            except OSError:
                unsafe += 1
                continue
            files.append(
                _OwnerAudioFile(
                    path=resolved,
                    size_bytes=max(0, int(file_stat.st_size)),
                    mtime=float(file_stat.st_mtime),
                )
            )
        files.sort(key=lambda item: (item.mtime, str(item.path)))
        return files, unsafe

    def _write_wav_atomic(self, wav_bytes: bytes, captured_at: datetime) -> Path:
        root = self._owner_root(create=True)
        day_dir = root / captured_at.strftime("%Y-%m-%d")
        if day_dir.is_symlink():
            raise RuntimeError("ambient day directory must not be a symlink")
        day_dir.mkdir(parents=True, exist_ok=True)
        day_dir = day_dir.resolve(strict=True)
        day_dir.relative_to(root)
        name = f"{captured_at.strftime('%H%M%S')}-{uuid.uuid4().hex[:12]}.wav"
        destination = day_dir / name
        temporary = day_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(wav_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination.resolve(strict=True)

    async def _delete_owner_audio(
        self,
        item: _OwnerAudioFile,
        known_record: AmbientAudioFileRecord | None,
    ) -> bool:
        """Delete one verified owner file and compensate its durable quota charge."""

        if known_record is not None and known_record.quota_charged and self._governor is None:
            self._stats.audio_delete_failed += 1
            logger.error("ambient GC cannot release charged file without quota governor")
            return False
        try:
            await asyncio.to_thread(item.path.unlink)
        except FileNotFoundError:
            return True
        except OSError as exc:
            self._stats.audio_delete_failed += 1
            logger.warning("ambient GC unlink failed: %s", exc)
            return False

        removed_record: AmbientAudioFileRecord | None = None
        public_registry_release = (
            self._audio_registry_enabled
            and self._repo is not None
            and self._principal.mode == "public"
            and self._governor is not None
        )
        if public_registry_release:
            assert self._governor is not None
            assert self._repo is not None
            try:
                released = await self._governor.release_registered_ambient_storage(
                    self._principal,
                    str(item.path),
                )
                if released is None:
                    # Unregistered legacy file: still detach any matching transcript ref.
                    await self._repo.delete_ambient_audio_file(str(item.path))
            except Exception as exc:
                # The absent file remains registered and is retried on the next GC pass.
                self._stats.audio_delete_failed += 1
                logger.warning("ambient GC atomic quota release failed: %s", exc)
        elif self._audio_registry_enabled and self._repo is not None:
            try:
                removed_record = await self._repo.delete_ambient_audio_file(str(item.path))
            except Exception as exc:
                self._stats.audio_delete_failed += 1
                logger.warning("ambient GC registry delete failed: %s", exc)
        if removed_record is not None and removed_record.quota_charged:
            # Defensive fallback for a charged row encountered outside public mode.
            if self._governor is None:
                self._stats.audio_delete_failed += 1
            else:
                try:
                    await self._governor.release_storage_bytes(
                        self._principal,
                        removed_record.size_bytes,
                    )
                except Exception as exc:
                    self._stats.audio_delete_failed += 1
                    logger.error("ambient GC quota release failed: %s", exc)

        self._stats.audio_files_deleted += 1
        self._stats.audio_bytes_deleted += item.size_bytes
        return True

    async def _reconcile_missing_audio(self, record: AmbientAudioFileRecord) -> None:
        if record.quota_charged and self._governor is None:
            self._stats.audio_delete_failed += 1
            return
        assert self._repo is not None
        try:
            if self._principal.mode == "public" and self._governor is not None:
                await self._governor.release_registered_ambient_storage(
                    self._principal,
                    record.audio_ref,
                )
            else:
                removed = await self._repo.delete_ambient_audio_file(record.audio_ref)
                if removed is not None and removed.quota_charged:
                    assert self._governor is not None
                    await self._governor.release_storage_bytes(self._principal, removed.size_bytes)
            self._stats.audio_missing_reconciled += 1
        except Exception as exc:
            self._stats.audio_delete_failed += 1
            logger.warning("ambient missing-file reconciliation failed: %s", exc)

    async def _garbage_collect_locked(  # noqa: PLR0912, PLR0915
        self,
        *,
        required_bytes: int,
        now: datetime,
    ) -> None:
        """Apply retention and owner capacity under the already-held storage lock."""

        owner_cap = self._settings.ambient_audio_owner_max_bytes
        if required_bytes > owner_cap:
            raise QuotaExceeded("storage_bytes", limit=owner_cap, used=0)

        try:
            files, unsafe_count = await asyncio.to_thread(self._scan_owner_wavs)
        except Exception as exc:
            self._stats.audio_gc_failed += 1
            logger.warning("ambient owner scan failed closed: %s", exc)
            if required_bytes:
                raise RuntimeError("ambient storage scan unavailable") from exc
            return
        self._stats.audio_delete_failed += unsafe_count

        records: list[AmbientAudioFileRecord] = []
        if self._audio_registry_enabled and self._repo is not None:
            try:
                records = await self._repo.list_ambient_audio_files()
            except Exception as exc:
                self._stats.audio_gc_failed += 1
                logger.warning("ambient registry scan failed closed: %s", exc)
                total = sum(item.size_bytes for item in files)
                if total + required_bytes > owner_cap:
                    raise QuotaExceeded("storage_bytes", limit=owner_cap, used=total) from exc
                return

        files_by_ref = {str(item.path): item for item in files}
        records_by_ref: dict[str, AmbientAudioFileRecord] = {}
        for record in records:
            safe_ref = self._safe_owner_reference(record.audio_ref, require_file=False)
            if safe_ref is None:
                self._stats.audio_delete_failed += 1
                continue
            normalized_ref = str(safe_ref)
            records_by_ref[normalized_ref] = record
            if not safe_ref.exists():
                await self._reconcile_missing_audio(record)
                records_by_ref.pop(normalized_ref, None)
            elif normalized_ref not in files_by_ref:
                # A directory/device/symlink named *.wav is never a GC candidate.
                self._stats.audio_delete_failed += 1

        cutoff = now.timestamp() - self._settings.ambient_audio_retention_s
        active = dict(files_by_ref)
        for ref, item in tuple(active.items()):
            if item.mtime >= cutoff:
                continue
            if await self._delete_owner_audio(item, records_by_ref.get(ref)):
                active.pop(ref, None)
                records_by_ref.pop(ref, None)

        total = sum(item.size_bytes for item in active.values())
        for ref, item in sorted(active.items(), key=lambda pair: (pair[1].mtime, pair[0])):
            if total + required_bytes <= owner_cap:
                break
            if await self._delete_owner_audio(item, records_by_ref.get(ref)):
                active.pop(ref, None)
                records_by_ref.pop(ref, None)
                total -= item.size_bytes
        if total + required_bytes > owner_cap:
            raise QuotaExceeded("storage_bytes", limit=owner_cap, used=total)

    async def collect_garbage(self) -> None:
        """Run owner-scoped retention/capacity GC without creating a storage directory."""

        async with self._storage_lock:
            await self._garbage_collect_locked(required_bytes=0, now=datetime.now(UTC))

    async def _cleanup_failed_store(
        self,
        *,
        audio_ref: str,
        registered: bool,
        reservation: QuotaReservation | None,
    ) -> None:
        if audio_ref:
            safe_path = self._safe_owner_reference(audio_ref, require_file=True)
            if safe_path is not None:
                try:
                    await asyncio.to_thread(safe_path.unlink)
                except OSError as exc:
                    logger.error("ambient failed-store cleanup could not unlink: %s", exc)
            if registered and self._repo is not None:
                try:
                    await self._repo.delete_ambient_audio_file(audio_ref)
                except Exception as exc:
                    logger.error("ambient failed-store cleanup could not remove registry: %s", exc)
        if reservation is not None:
            try:
                await reservation.release()
            except Exception as exc:
                logger.error("ambient failed-store cleanup could not release quota: %s", exc)

    async def _persist_quality_wav(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        captured_at: datetime,
        *,
        prepared_wav: bytes | None = None,
        prepared_reservation: QuotaReservation | None = None,
    ) -> str:
        """Encode, reserve exact bytes, atomically write, register and settle."""

        if prepared_wav is None:
            try:
                wav_bytes = await asyncio.to_thread(
                    pcm_to_wav,
                    audio_bytes,
                    sample_rate=sample_rate,
                )
            except Exception:
                self._stats.audio_store_failed += 1
                raise
        else:
            wav_bytes = prepared_wav
        size_bytes = len(wav_bytes)
        async with self._storage_lock:
            try:
                await self._garbage_collect_locked(
                    required_bytes=size_bytes,
                    now=captured_at,
                )
            except QuotaExceeded:
                self._stats.audio_store_failed += 1
                self._stats.audio_quota_rejected += 1
                raise
            except Exception:
                self._stats.audio_store_failed += 1
                raise

            reservation = prepared_reservation
            audio_ref = ""
            registered = False
            try:
                if self._governor is not None and reservation is None:
                    reservation = await self._governor.reserve_storage(
                        self._principal,
                        size_bytes,
                    )
                path = await asyncio.to_thread(
                    self._write_wav_atomic,
                    wav_bytes,
                    captured_at,
                )
                audio_ref = str(path)
                if self._audio_registry_enabled and self._repo is not None:
                    await self._repo.register_ambient_audio_file(
                        audio_ref=audio_ref,
                        size_bytes=size_bytes,
                        captured_at=captured_at,
                        quota_charged=(
                            self._principal.mode == "public" and self._governor is not None
                        ),
                    )
                    registered = True
                if reservation is not None:
                    await reservation.settle(size_bytes)
            except QuotaExceeded:
                await self._cleanup_failed_store(
                    audio_ref=audio_ref,
                    registered=registered,
                    reservation=reservation,
                )
                self._stats.audio_store_failed += 1
                self._stats.audio_quota_rejected += 1
                raise
            except Exception:
                await self._cleanup_failed_store(
                    audio_ref=audio_ref,
                    registered=registered,
                    reservation=reservation,
                )
                self._stats.audio_store_failed += 1
                raise

        self._stats.audio_files_stored += 1
        self._stats.audio_bytes_stored += size_bytes
        self._stats.last_audio_stored_at = captured_at.isoformat()
        return audio_ref

    async def _prepare_quality_wav(
        self,
        audio_bytes: bytes,
        sample_rate: int,
    ) -> tuple[bytes, QuotaReservation | None]:
        """Encode once and reject a known public storage overflow before ASR."""

        try:
            wav_bytes = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        except Exception:
            self._stats.audio_store_failed += 1
            raise
        if self._governor is None or self._principal.mode != "public":
            return wav_bytes, None
        try:
            reservation = await self._governor.reserve_storage(
                self._principal,
                len(wav_bytes),
            )
        except QuotaExceeded:
            self._stats.audio_store_failed += 1
            self._stats.audio_quota_rejected += 1
            raise
        except Exception:
            self._stats.audio_store_failed += 1
            raise
        handle = _CURRENT_ADMISSION.get()
        if handle is not None:
            handle.storage_reservation = reservation
        return wav_bytes, reservation

    @_ordered_capture_admission
    async def ingest_chunk(  # noqa: PLR0912, PLR0915
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
        capture_mode: str = "free",
        asr_context: ASRRequestContext | None = None,
        client_segment_id: str | None = None,
        capture_operation_key: str | None = None,
        request_fingerprint: str | None = None,
        captured_at: datetime | None = None,
    ) -> CaptureChunkResult:
        if (capture_operation_key is None) != (request_fingerprint is None):
            raise ValueError(
                "capture operation key and request fingerprint must be provided together"
            )
        terminal = await self._terminal_meeting_capture(meeting_id)
        if terminal is not None:
            return terminal
        if self._state is not None and not self._settings.public_demo_mode:
            await self._state.hydrate()
            self._state.start_watchdog()
        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        audio_bytes = normalized.pcm
        sample_rate = normalized.sample_rate
        captured_dt = captured_at or datetime.now(UTC)
        if captured_dt.tzinfo is None:
            captured_dt = captured_dt.replace(tzinfo=UTC)
        captured_at = captured_dt.isoformat()
        audio_ref = ""
        # M_diag_brake：每条 ingest_chunk 头部记一次（含所有末态），
        # 后端日志即使只看 chunks_total 也能粗略知道 firehose 多大。
        self._stats.chunks_total += 1
        self._stats.stats_sequence += 1
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
        self._stats.last_rms = round(gate.rms, 2)
        self._stats.last_speech_ratio = round(gate.speech_ratio, 4)
        self._stats.last_gate_reason = _stable_gate_reason(gate.reason)
        observed_frames = _complete_admission_frames(audio_bytes)
        accepted_frames = 0
        if gate.pass_ and observed_frames > 0:
            accepted_frames = min(
                observed_frames,
                max(0, round(observed_frames * gate.speech_ratio)),
            )
        self._stats.observed_audio_frames += observed_frames
        self._stats.accepted_speech_frames += accepted_frames
        self._stats.accepted_speech_ratio = (
            round(
                self._stats.accepted_speech_frames / self._stats.observed_audio_frames,
                4,
            )
            if self._stats.observed_audio_frames > 0
            else 0.0
        )
        audio_duration_ms = int(len(audio_bytes) / max(1, sample_rate * 2) * 1000)
        active_speech_ms = round(audio_duration_ms * gate.speech_ratio)
        coherent_speech_ms = 0
        if gate.pass_:
            coherent_speech_ms = max(
                (
                    segment.duration_ms
                    for segment in split_into_voiced_segments(
                        audio_bytes,
                        frame_rms_threshold=self._settings.ambient_frame_rms_threshold,
                    )
                ),
                default=0,
            )
        strong_speech_evidence = (
            gate.pass_
            and gate.speech_ratio >= self._settings.automeet_min_valid_speech_ratio
            and active_speech_ms >= self._settings.automeet_min_valid_speech_ms
            and coherent_speech_ms >= self._settings.automeet_min_valid_speech_ms
        )
        # Watchdog 必须以已经进入采集链路的强声学证据为准，不能等远端 ASR
        # 排队/终态。这里只续命已存在的 auto meeting，不参与自动开始，也不
        # 放行或持久化任何文本。
        if (
            strong_speech_evidence
            and self._state is not None
            and not self._settings.public_demo_mode
        ):
            try:
                await self._state.note_acoustic_activity(
                    now=captured_dt,
                    meeting_id=meeting_id,
                )
            except Exception as exc:
                logger.warning("meeting acoustic heartbeat failed: %s", type(exc).__name__)

        prepared_wav: bytes | None = None
        prepared_reservation: QuotaReservation | None = None
        if gate.pass_:
            prepared_wav, prepared_reservation = await self._prepare_quality_wav(
                audio_bytes,
                sample_rate,
            )

        stt_segs: list[TranscriptSegment] = []
        speaker_id: str | None = None
        diar_task: asyncio.Task[str | None] | None = None
        transcript_stream: TranscriptStreamHandler | None = None
        # M_diag_brake：默认 ok，后续每个分支按需覆写。
        stt_status: SttStatus = "ok"
        # STT 与 ECAPA 同时启动，但 canonical transcript 不等待 ECAPA；说话人只作
        # 后台 enrichment。后续幻觉/重复门拒绝文本时会取消仍在运行的任务，且不
        # 发布或持久化 speaker enrichment，因此 ECAPA 迟到不能放行无效转写。
        if gate.pass_:
            if self._diarizer is not None:
                diar_task = asyncio.create_task(
                    self._safe_diarize(audio_bytes, sample_rate, meeting_id=meeting_id),
                    name="capture-ecapa-enrichment",
                )
            try:
                transcript_stream = self._transcript_stream_handler(
                    meeting_id=meeting_id,
                    capture_operation_key=capture_operation_key,
                )
                stt_segs = await self._safe_stt(
                    audio_bytes,
                    sample_rate,
                    context=asr_context,
                    on_partial=transcript_stream,
                )
            except _STTCircuitOpenError:
                # canonical Qwen ASR 调度器已熔断；不再发起请求 → 前端应进入指数退避
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
            elif gate.reason == "stationary_noise":
                self._stats.gated_stationary_noise += 1
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
        # 从此处起的 diarize、重复门、MeetingState、落盘与投影都必须按入口
        # ordinal 提交；ASR 在此之前未持有该 lane，因而仍可并发执行。
        await self._enter_admission_commit()
        if gate.pass_:
            audio_ref = await self._persist_quality_wav(
                audio_bytes,
                sample_rate,
                captured_dt,
                prepared_wav=prepared_wav,
                prepared_reservation=prepared_reservation,
            )
        else:
            # 静音流仍可触发 retention；GC 不创建 owner directory，异常也不改变
            # 本 chunk 的 gated 结果。
            try:
                await self.collect_garbage()
            except Exception as exc:
                logger.warning("ambient gated-chunk GC skipped: %s", exc)

        ambient_stored = False
        ambient_text: str | None = None
        ambient_segment_id: int | None = None
        texts = [s.text.strip() for s in stt_segs if s.text.strip()]
        recent_strong_speech = (
            self._last_strong_speech_at is not None
            and (captured_dt - self._last_strong_speech_at).total_seconds()
            <= _RECENT_STRONG_SPEECH_WINDOW_S
        )

        # Gate 3：STT 调用成功但返回空文本（音频里 ASR 没"听到"任何字）
        if gate.pass_ and stt_status == "ok" and not texts:
            self._stats.stt_empty += 1
            stt_status = "empty"

        # ── 后置 STT 幻觉门控 ──
        hallu_drop = False
        repeated_for_meeting = False
        if texts:
            joined = " ".join(texts)
            hallu, why = is_likely_hallucination(
                joined,
                audio_bytes,
                max_cps=self._settings.ambient_max_cps,
                min_chars=self._settings.ambient_min_stt_chars,
                speech_duration_s=active_speech_ms / 1000,
                acoustic_speech_ratio=gate.speech_ratio,
                coherent_speech_ms=coherent_speech_ms,
                recent_strong_speech=recent_strong_speech,
                min_acoustic_speech_ratio=self._settings.automeet_min_valid_speech_ratio,
                min_coherent_speech_ms=self._settings.automeet_min_valid_speech_ms,
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

        # 跨 chunk 重复门：第二次相同签名仍保留一次供用户核对，但不再作为
        # meeting 活跃证据；达到 drop_after 后，新副本不再污染转录/RAG。
        if texts:
            joined = " ".join(texts)
            repeated_for_meeting, repeat_drop, previous_count = self._record_transcript_repeat(
                joined, captured_at=captured_dt
            )
            if repeat_drop:
                logger.debug(
                    "ambient repeat drop: previous=%d text=%r",
                    previous_count,
                    joined,
                )
                texts = []
                stt_segs = []
                self._stats.repeat_dropped += 1

        if texts and strong_speech_evidence and not repeated_for_meeting:
            self._last_strong_speech_at = captured_dt

        # ── STT 后处理：LLM 加标点 + 分段（fail-soft） ──
        # 仅当：通过幻觉门控（确认 STT 文本有意义）+ punctuator 注入 + flag 打开 时执行。
        # 失败 / 超时 → 退回原 stt_segs，不影响 counter / 主链路。
        # 不动 stored counter 语义：本步只重写 `.text`，不删段、不加段。
        if texts and not hallu_drop and self._punctuator is not None and self._punctuator.enabled:
            try:
                stt_segs = await self._punctuator.punctuate(stt_segs)
                texts = [s.text.strip() for s in stt_segs if s.text.strip()]
            except Exception as e:
                # 多一道兜底：punctuator 内部已有 try/except，但仍守住主链路。
                logger.warning("ambient punctuator pipeline error: %s", e)

        # STT adapter 只能投影 provider 的 provisional/final 文本，不知道后续
        # hallucination/repeat gate 的最终接纳结果。若 canonical pipeline 丢弃
        # 该文本，立即发布空 completed 终态，避免界面把“嗯”等 provisional
        # 文本一直保留到 durable receipt 的迟到响应。
        if not texts and transcript_stream is not None:
            try:
                await transcript_stream(TranscriptStreamEvent(text="", state="completed"))
            except Exception as exc:
                logger.warning(
                    "ambient transcript dismissal projection failed: %s",
                    type(exc).__name__,
                )

        # phase4-speaker-reset：把 meeting context 传给 registry，让 per-meeting
        # counter 工作。优先级：
        #   1. caller 显式 meeting_id（manual meeting 走这条）
        #   2. meeting_state.current.meeting_id（已在进行中的 auto/manual meeting）
        #   3. None → registry 内部走 ``__ambient__`` sentinel
        # 注：observe_chunk 还没跑（在下面）；本 chunk 触发的新 auto-meeting 在本
        # 行无法预知 → 只能落入 ``__ambient__`` 池。下一 chunk 起 state.current 就
        # 不为 None，会正确路由到新 meeting 的 counter。
        ctx_meeting_id: str | None = meeting_id
        if (
            ctx_meeting_id is None
            and self._state is not None
            and not self._settings.public_demo_mode
        ):
            current = self._state.current
            if current is not None:
                ctx_meeting_id = current.meeting_id

        speaker_label: str | None = "未知说话人" if texts else None

        if texts:
            ambient_text = " ".join(texts)
            # ambient row描述的是这次采集的规范音频边界，不是 provider 可能漂移的
            # STT 末段时间；meeting_segments 才保存模型的段内 start/end。
            duration_ms = audio_duration_ms
            repository_stored: bool | None = None
            if self._repo is not None:
                try:
                    if capture_operation_key is not None:
                        assert request_fingerprint is not None
                        append_result = await self._repo.append_capture_ambient_segment(
                            audio_ref=audio_ref,
                            text=ambient_text,
                            captured_at=captured_dt,
                            speaker_id=speaker_id,
                            speaker_label=speaker_label,
                            duration_ms=duration_ms,
                            client_segment_id=client_segment_id,
                            capture_operation_key=capture_operation_key,
                            request_fingerprint=request_fingerprint,
                        )
                        stored_id = append_result.segment_id
                        if not append_result.inserted:
                            canonical = await self._repo.get_ambient_segment(stored_id)
                            if canonical is None:
                                raise RuntimeError(
                                    "capture ambient canonical row disappeared during replay"
                                )
                            ambient_text = canonical.text
                            audio_ref = canonical.audio_ref
                            speaker_id = canonical.speaker_id
                            speaker_label = canonical.speaker_label
                    else:
                        stored_id = await self._repo.append_ambient_segment(
                            audio_ref=audio_ref,
                            text=ambient_text,
                            captured_at=captured_dt,
                            speaker_id=speaker_id,
                            speaker_label=speaker_label,
                            duration_ms=duration_ms,
                            client_segment_id=client_segment_id,
                        )
                    if not isinstance(stored_id, int) or stored_id <= 0:
                        raise RuntimeError("ambient repository returned an invalid segment id")
                    ambient_segment_id = stored_id
                    repository_stored = True
                except Exception as e:
                    repository_stored = False
                    self._stats.segment_store_failed += 1
                    logger.warning("ambient repo persist failed: %s", e)
                    raise AmbientPersistenceError("ambient persistence unavailable") from e
            rag_stored = False
            # SQLite is authoritative and inserts ``index_pending`` with the
            # canonical row. Projection is replayable by stable segment id, so
            # it must not extend the capture admission/receipt critical path.
            if repository_stored and ambient_segment_id is not None:
                self._schedule_rag_projection(
                    segment_id=ambient_segment_id,
                    text=ambient_text,
                    captured_at=captured_at,
                    audio_ref=audio_ref,
                    speaker_id=speaker_id,
                    speaker_label=speaker_label,
                )
            elif repository_stored is None:
                # Explicit repository-free adapters have no durable repair
                # intent; preserve their historical synchronous success signal.
                try:
                    await self._rag.ingest_ambient_segment(
                        ambient_text,
                        captured_at=captured_at,
                        audio_ref=audio_ref,
                        speaker_id=speaker_id,
                        speaker_label=speaker_label,
                        operation_id=None,
                    )
                    rag_stored = True
                except Exception as exc:
                    logger.warning("ambient RAG ingest failed: %s", exc)

            # DB 是正常运行时 authoritative store；RAG 是可修复 projection。
            # 仅在没有 repository 的显式降级/单测模式下才沿用 RAG 成功语义。
            ambient_stored = repository_stored if repository_stored is not None else rag_stored
            if ambient_stored:
                self._stats.stored += 1
                self._stats.last_stored_at = captured_at
                if ambient_segment_id is not None and ambient_text is not None:
                    self._schedule_recognized_memory(
                        text=ambient_text,
                        segment_id=ambient_segment_id,
                        captured_at=captured_dt,
                        meeting_id=ctx_meeting_id,
                    )
            else:
                self._stats.segment_store_failed += 1

        # 自动会议检测：交给 MeetingState（单例状态机）；它内部协调 detector。
        # ambient 主链路只负责"喂观测"，状态/落库由 MeetingState 全权决定。
        has_meeting_audio_evidence = (
            gate.pass_
            and gate.speech_ratio >= self._settings.automeet_min_valid_speech_ratio
            and active_speech_ms >= self._settings.automeet_min_valid_speech_ms
            and coherent_speech_ms >= self._settings.automeet_min_valid_speech_ms
        )
        has_speech_result = bool(texts) or stt_status in {"failed", "circuit_open"}
        meeting_valid_speech = (
            has_meeting_audio_evidence
            and has_speech_result
            and not hallu_drop
            and not repeated_for_meeting
        )
        effective_meeting_id: str | None = meeting_id
        if self._state is not None and not self._settings.public_demo_mode:
            try:
                if meeting_id is None:
                    effective_meeting_id = await self._state.observe_chunk(
                        speaker_id=speaker_id,
                        duration_ms=active_speech_ms if meeting_valid_speech else 0,
                        now=captured_dt,
                        chunk_started_at=captured_dt
                        - timedelta(milliseconds=audio_duration_ms),
                        is_valid_speech=meeting_valid_speech,
                    )
                else:
                    await self._state.note_valid_speech(
                        meeting_id,
                        now=captured_dt,
                        is_valid_speech=meeting_valid_speech,
                    )
            except Exception as e:
                logger.warning("meeting_state.observe_chunk failed: %s", e)

        meeting_segments = []
        meeting_terminal = False
        if effective_meeting_id and texts:
            try:
                meeting_segments = await self._meeting.ingest_from_stt(
                    effective_meeting_id,
                    audio_bytes,
                    stt_segs,
                    sample_rate=sample_rate,
                    capture_operation_key=capture_operation_key,
                    request_fingerprint=request_fingerprint,
                    captured_at=captured_dt,
                    source_ambient_segment_id=ambient_segment_id,
                    initial_speaker_id=None,
                    initial_speaker_label="未知说话人",
                )
                if capture_operation_key is not None and self._repo is not None:
                    canonical_meeting_id = await self._repo.get_capture_meeting_id(
                        capture_operation_key=capture_operation_key
                    )
                    if canonical_meeting_id is not None:
                        effective_meeting_id = canonical_meeting_id
            except MeetingPipelineError as e:
                if getattr(e, "code", None) == "meeting_not_active":
                    terminal_after_race = await self._terminal_meeting_capture(
                        effective_meeting_id
                    )
                    if terminal_after_race is not None:
                        stt_status = "terminal_ignored"
                        meeting_segments = []
                        meeting_terminal = True
                    else:
                        logger.debug("meeting overlay skipped: %s", e)
                else:
                    logger.debug("meeting overlay skipped: %s", e)

        if diar_task is not None:
            if (
                not meeting_terminal
                and texts
                and (ambient_segment_id is not None or effective_meeting_id is not None)
            ):
                self._schedule_speaker_enrichment(
                    diar_task,
                    ambient_segment_id=ambient_segment_id,
                    meeting_id=effective_meeting_id,
                    capture_operation_key=capture_operation_key,
                    request_fingerprint=request_fingerprint,
                    captured_at=captured_dt,
                    ctx_meeting_id=ctx_meeting_id,
                )
            else:
                diar_task.cancel()
                await asyncio.gather(diar_task, return_exceptions=True)

        return CaptureChunkResult(
            segment_id=client_segment_id,
            ambient_segment_id=ambient_segment_id,
            ambient_stored=ambient_stored,
            ambient_text=ambient_text,
            audio_ref=audio_ref,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
            speaker_status=(
                "pending" if texts and diar_task is not None else "unknown" if texts else None
            ),
            meeting_id=effective_meeting_id,
            meeting_segments=meeting_segments,
            stt_status=stt_status,
            capture_mode=(
                "formal"
                if capture_mode == "formal" and meeting_id is not None
                else "auto"
                if effective_meeting_id is not None
                else "free"
            ),
        )

    async def _terminal_meeting_capture(
        self,
        meeting_id: str | None,
    ) -> CaptureChunkResult | None:
        """Complete a durable receipt without reprocessing a terminal meeting.

        Recovery can replay a g3 chunk after the meeting was ended or its
        minutes generation failed.  That request is a terminal no-op: it must
        not invoke ASR, append a segment, or bubble a stale in-memory pipeline
        race into a 5xx (which would keep the renderer item in retry).
        """

        if meeting_id is None or self._repo is None:
            return None
        getter = getattr(self._repo, "get_meeting", None)
        if not callable(getter):
            return None
        record = await getter(meeting_id)
        if not isinstance(record, MeetingRecord):
            return None
        if record.state == "in_meeting" and record.minutes_status not in {
            "ok",
            "generation_failed",
            "no_content",
        }:
            return None
        logger.info(
            "capture_terminal_ignored meeting_id=%s state=%s minutes_status=%s",
            meeting_id,
            record.state,
            record.minutes_status,
        )
        return CaptureChunkResult(
            segment_id=None,
            meeting_id=meeting_id,
            meeting_segments=[],
            stt_status="terminal_ignored",
            capture_mode="formal",
        )

    def _schedule_speaker_enrichment(
        self,
        diar_task: asyncio.Task[str | None],
        *,
        ambient_segment_id: int | None,
        meeting_id: str | None,
        capture_operation_key: str | None,
        request_fingerprint: str | None,
        captured_at: datetime,
        ctx_meeting_id: str | None,
    ) -> None:
        async def run() -> None:
            status = "unknown"
            speaker_id: str | None = None
            try:
                speaker_id = await diar_task
                if speaker_id is not None and self._registry is not None:
                    label = await self._registry.label_for(
                        speaker_id, captured_at=captured_at, meeting_id=ctx_meeting_id
                    )
                    status = "identified"
                else:
                    label = "未知说话人"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("speaker enrichment failed: %s", type(exc).__name__)
                label = "未知说话人"
                status = "failed"
            try:
                if ambient_segment_id is not None and self._repo is not None:
                    await self._repo.update_ambient_segment_speaker(
                        ambient_segment_id, speaker_id=speaker_id, speaker_label=label
                    )
                if meeting_id is not None and callable(
                    getattr(self._meeting, "enrich_capture_speaker", None)
                ):
                    await self._meeting.enrich_capture_speaker(
                        meeting_id,
                        capture_operation_key=capture_operation_key,
                        request_fingerprint=request_fingerprint,
                        speaker_id=speaker_id,
                        speaker_label=label,
                        # Capture enrichment owns this event so the Ambient and
                        # Meeting layers cannot publish duplicate updates.
                        publish_event=False,
                    )
                if self._event_bus is not None:
                    operation_key = (
                        f"capture-{capture_operation_key[:16]}"
                        if capture_operation_key
                        else f"ambient-{ambient_segment_id}"
                        if ambient_segment_id is not None
                        else None
                    )
                    await self._event_bus.publish(
                        EchoEvent(
                            type="meeting.speaker_updated",
                            meeting_id=meeting_id,
                            payload={
                                "ambient_segment_id": ambient_segment_id,
                                "speaker_id": speaker_id,
                                "speaker_label": label,
                                "speaker_status": status,
                                "capture_operation_key": operation_key,
                                "operation_key": operation_key,
                            },
                        )
                    )
            except Exception as exc:
                logger.warning("speaker enrichment update failed: %r", exc)

        task = asyncio.create_task(run(), name="capture-speaker-enrichment")
        self._speaker_enrichment_tasks.add(task)
        task.add_done_callback(self._speaker_enrichment_tasks.discard)

    async def _safe_stt(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        *,
        context: ASRRequestContext | None = None,
        on_partial: TranscriptStreamHandler | None = None,
    ) -> list:  # type: ignore[type-arg]
        """STT 调用 + typed exception 分流（M_diag_brake）。

        调用方需要区分"熔断（前端必须停止上传）"和"单次失败（前端可继续）"。
        public demo 里 eight STT 偶发 20~60s 慢响应时，最危险的是前端 6s
        一片持续并发上传，最终把慢请求堆成超时风暴；所以这里采用 non-blocking
        single-flight：上一条 STT 还没结束时，本 chunk 快速标记为 failed，
        不再额外打 eight，也不触发前端长时间熔断倒计时。

        熔断识别只保留 legacy 兼容：如果某个 STT port 明确抛出含
        "circuit open" 的异常，就继续暴露为 circuit_open；canonical ASR adapter
        本身不再主动打开本地熔断器。
        """
        if self._settings.asr_scheduler_enabled and self._asr_scheduler is not None:
            request_context = self._server_asr_context(context)
            transcribe = self._asr_scheduler.transcribe
            kwargs: dict[str, object] = {
                "sample_rate": sample_rate,
                "context": request_context,
            }
            if on_partial is not None and "on_partial" in inspect.signature(transcribe).parameters:
                kwargs["on_partial"] = on_partial
            try:
                return await transcribe(audio_bytes, **kwargs)  # type: ignore[arg-type]
            except ASRErrorBase as exc:
                # A scheduler/provider terminal belongs to this audio chunk,
                # not to the durable capture transport.  Convert it to the
                # existing per-chunk failed result so /capture/chunk can commit
                # a receipt and the following 15s chunks keep draining.
                logger.warning("ambient STT failed (audio saved): %s", exc)
                raise _STTCallFailedError(str(exc)) from exc

        if self._stt_lock.locked():
            msg = "stt busy: previous request still running"
            logger.warning("ambient STT busy (audio saved): %s", msg)
            raise _STTCallFailedError(msg)

        async with self._stt_lock:
            started_at = time.monotonic()
            request_context = self._server_asr_context(context)
            try:
                transcribe = self._stt.transcribe
                kwargs = {"sample_rate": sample_rate}
                if on_partial is not None and "on_partial" in inspect.signature(transcribe).parameters:
                    kwargs["on_partial"] = on_partial
                result = await transcribe(audio_bytes, **kwargs)  # type: ignore[arg-type]
            except Exception as e:
                if isinstance(e, ASRErrorBase):
                    await self._record_direct_telemetry(
                        request_context,
                        audio_bytes=audio_bytes,
                        sample_rate=sample_rate,
                        started_at=started_at,
                        error=e,
                    )
                    raise
                msg = str(e)
                if "circuit open" in msg.lower():
                    logger.warning("ambient STT circuit open (audio saved): %s", e)
                    error: RuntimeError = _STTCircuitOpenError(msg)
                    await self._record_direct_telemetry(
                        request_context,
                        audio_bytes=audio_bytes,
                        sample_rate=sample_rate,
                        started_at=started_at,
                        error=error,
                    )
                    raise error from e
                logger.warning("ambient STT failed (audio saved): %s", e)
                error = _STTCallFailedError(msg)
                await self._record_direct_telemetry(
                    request_context,
                    audio_bytes=audio_bytes,
                    sample_rate=sample_rate,
                    started_at=started_at,
                    error=error,
                )
                raise error from e
            await self._record_direct_telemetry(
                request_context,
                audio_bytes=audio_bytes,
                sample_rate=sample_rate,
                started_at=started_at,
                error=None,
            )
            return result

    def _server_asr_context(self, context: ASRRequestContext | None) -> ASRRequestContext:
        principal = self._principal
        incoming = context or ASRRequestContext(request_id=f"ambient-{uuid.uuid4().hex}")
        return ASRRequestContext(
            request_id=incoming.request_id,
            idempotency_key=incoming.idempotency_key,
            tenant_id=principal.tenant_id,
            principal_id=principal.user_id,
            device_id=principal.device_id,
            deadline_s=min(
                incoming.deadline_s or self._settings.asr_job_deadline_s,
                self._settings.asr_job_deadline_s,
            ),
            capability=incoming.capability or "ambient_capture",
            platform=incoming.platform,
            app_version=incoming.app_version,
            options=incoming.options,
        )

    async def _record_direct_telemetry(
        self,
        context: ASRRequestContext,
        *,
        audio_bytes: bytes,
        sample_rate: int,
        started_at: float,
        error: BaseException | None,
    ) -> None:
        if self._telemetry is None:
            return
        await self._telemetry.record_asr(
            context=context,
            provider="model_gateway",
            success=error is None,
            error=error,
            latency_ms=round((time.monotonic() - started_at) * 1000),
            queue_wait_ms=0,
            audio_duration_ms=round(len(audio_bytes) / 2 / max(1, sample_rate) * 1000),
        )

    async def _safe_diarize(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        *,
        meeting_id: str | None = None,
    ) -> str | None:
        """声纹识别 ambient 入口（spk-2 改为走句级切片接口）。

        改前：整段 6s chunk 一次 embed → 多人混音 / 噪声主导时被判新人。
        改后：identify_segments 在内部按 VAD 切段、每段独立 embed + EMA；本函数取
              "时长加权主导 speaker"（也即整 chunk 里说得最久的人）作为 chunk 的代表。

        若 diarizer 没实现 identify_segments（NullDiarizer 之外）则降级回老 identify。

        phase4-diar-deep：透传 meeting_id 给 diarizer，让活跃说话人 list 按会议隔离。
        meeting_id=None（ambient 主链路绝大多数情况）→ 共享 "_ambient" 池。
        计数器区分两条 None 路径：
        - diarize_returned_none：diarizer 正常跑了但说不出（短段无匹配 / 切不出 voiced）
        - diarize_failed：diarizer 抛异常
        """
        if self._diarizer is None:
            return None
        try:
            if hasattr(self._diarizer, "identify_segments"):
                segs = await self._diarizer.identify_segments(
                    audio_bytes,
                    sample_rate=sample_rate,
                    meeting_id=meeting_id,
                )
                if not segs:
                    self._stats.diarize_returned_none += 1
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
                    self._stats.diarize_returned_none += 1
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
            sid = await self._diarizer.identify(
                audio_bytes,
                sample_rate=sample_rate,
                meeting_id=meeting_id,
            )
            if sid is None:
                self._stats.diarize_returned_none += 1
            return sid
        except Exception as e:
            self._stats.diarize_failed += 1
            logger.warning("ambient diarizer failed: %s", e)
            return None
