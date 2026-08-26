"""Repository Port：本地持久化的抽象接口（meeting / ambient / speakers）。

业务（use_cases）只依赖此 Protocol；adapter（如 SQLite）在 adapters/repo 实现。

设计原则：
- repository 是**可选**依赖（None 时退化为纯内存，保持现有测试通过）
- 所有方法 async（即使 SQLite 是同步驱动）便于 adapter 使用 aiosqlite
- 不暴露 DB cursor / connection，只暴露领域操作
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.schemas.meeting import TranscriptSegment

MeetingState = Literal["in_meeting", "ended", "finalized"]

# 纪要生成状态（meetings.minutes_status 列；migration 003）
# - None：会议进行中或从未尝试 finalize
# - "generating"：finalize 正在跑（兜底，正常情况下从 in_meeting 直接进 ok/failed）
# - "ok"：已成功生成（与 state="finalized" 同步）
# - "generation_failed"：LLM 失败 / JSON 校验失败，用户可重试
# - "no_content"：会议已结束，但没有可总结的有效转写；不是生成失败
MinutesStatus = Literal["generating", "ok", "generation_failed", "no_content"]
RagProjectionState = Literal[
    "reconcile_pending",
    "index_pending",
    "indexed",
    "index_failed",
    "delete_pending",
    "deleted",
    "delete_failed",
]
CaptureClaimStatus = Literal["acquired", "in_progress", "completed"]


class CaptureIdempotencyConflict(RuntimeError):
    """同一 capture 身份键被用于不同请求指纹。"""


class CaptureClaimLost(RuntimeError):
    """处理者的 capture fence 已过期或已被新的 takeover 替代。"""


class MeetingRecord(BaseModel):
    """落库的 meeting 行（不含 segments）。"""

    id: str
    title: str | None = None
    state: MeetingState
    started_at: datetime
    ended_at: datetime | None = None
    finalized_at: datetime | None = None
    auto_started: bool = False
    minutes_json: str | None = None
    raw_transcript_ref: str | None = None
    minutes_status: MinutesStatus | None = None
    minutes_error: str | None = None
    # M_minutes_refactor (migration 004)：LLM finalize 时生成的语义化标题
    # （≤ 18 字，中文），独立列方便 GET /meetings 不解析 minutes_json blob。
    display_title: str | None = None
    # Durable user intent: automatic startup recovery must not regenerate minutes
    # that were explicitly cleared. A later explicit finalize clears this marker.
    minutes_cleared_at: datetime | None = None
    rag_projection_state: RagProjectionState | None = None
    rag_projection_error: str | None = None
    rag_projected_at: datetime | None = None
    rag_projection_attempts: int = 0
    rag_projection_next_retry_at: datetime | None = None
    rag_projection_generation: int = 0
    minutes_generation_run_id: str | None = None
    minutes_generation_cancelled_at: datetime | None = None


class MeetingCreateResult(BaseModel):
    """Authoritative active snapshot plus this transaction's insert disposition."""

    meeting: MeetingRecord
    created: bool


class AmbientSegmentRecord(BaseModel):
    """ambient_segments 单行。"""

    id: int = 0
    audio_ref: str
    text: str
    speaker_id: str | None = None
    speaker_label: str | None = None
    duration_ms: int = 0
    captured_at: datetime
    device_id: str | None = None
    client_segment_id: str | None = None
    rag_projection_state: RagProjectionState | None = None
    rag_projection_error: str | None = None
    rag_projected_at: datetime | None = None
    rag_projection_attempts: int = 0
    rag_projection_next_retry_at: datetime | None = None
    capture_operation_key: str | None = None
    capture_request_fingerprint: str | None = None


class AmbientAudioFileRecord(BaseModel):
    """Owner-scoped ambient WAV inventory row (migration 027)."""

    audio_ref: str
    size_bytes: int
    captured_at: datetime
    quota_charged: bool = False


class CaptureRequestClaim(BaseModel):
    """不含正文的 durable capture claim/receipt 投影。"""

    status: CaptureClaimStatus
    operation_key_hash: str
    request_fingerprint: str
    lease_holder: str | None = None
    lease_fence: int = 0
    lease_expires_at: float = 0.0
    ambient_stored: bool = False
    ambient_segment_id: int | None = None
    meeting_id: str | None = None
    stt_status: str | None = None
    capture_mode: str | None = None


class CaptureReceiptSettlement(BaseModel):
    """不含正文的 stop 前 capture 收尾结果。"""

    initial_pending: int = 0
    remaining_pending: int = 0
    waited_ms: float = 0.0
    timed_out: bool = False


class CaptureAmbientAppendResult(BaseModel):
    """capture ambient 规范行的原子插入/重放结果。"""

    segment_id: int
    inserted: bool


class CaptureMeetingAppendResult(BaseModel):
    """一次 capture 对 meeting 规范段落的原子插入/重放结果。"""

    accepted: bool
    inserted: bool
    segments: list[TranscriptSegment] = Field(default_factory=list)


class AmbientMeetingImportResult(BaseModel):
    """Canonical meeting projection of one or more ambient source rows."""

    accepted: bool
    inserted_count: int = 0
    segments: list[TranscriptSegment] = Field(default_factory=list)
    inserted_segments: list[TranscriptSegment] = Field(default_factory=list)


class SpeakerProfileRecord(BaseModel):
    """全局 speaker registry 行。"""

    speaker_id: str
    label: str | None = None
    n_samples: int = 0
    first_seen_at: datetime
    last_seen_at: datetime
    embedding_blob: bytes | None = Field(default=None, exclude=True)


class RepositoryPort(Protocol):
    """本地持久化抽象。

    Lifecycle:
    - ``init()`` 在 FastAPI lifespan startup 调
    - ``aclose()`` 在 shutdown 调
    """

    async def init(self) -> None: ...

    async def aclose(self) -> None: ...

    # ── Meetings ─────────────────────────────────────────────────
    async def create_meeting(
        self,
        meeting_id: str,
        *,
        started_at: datetime,
        title: str | None = None,
        auto_started: bool = False,
        state_event_reason: str | None = None,
    ) -> MeetingRecord:
        """Create or return the active meeting and stage its start events atomically."""
        ...

    async def create_meeting_boundary(
        self,
        meeting_id: str,
        *,
        started_at: datetime,
        title: str | None = None,
        auto_started: bool = False,
        state_event_reason: str | None = None,
    ) -> MeetingCreateResult:
        """Return the authoritative snapshot and whether this call inserted it."""
        ...

    async def update_meeting_state(
        self,
        meeting_id: str,
        *,
        state: MeetingState,
        title: str | None = None,
        ended_at: datetime | None = None,
        finalized_at: datetime | None = None,
        minutes_json: str | None = None,
        raw_transcript_ref: str | None = None,
        minutes_status: MinutesStatus | None = None,
        minutes_error: str | None = None,
        display_title: str | None = None,
        rag_projection_state: RagProjectionState | None = None,
        rag_projection_error: str | None = None,
        rag_projected_at: datetime | None = None,
    ) -> int | None:
        """Update a meeting and return its committed RAG projection generation."""
        ...

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None: ...

    async def list_meetings(
        self,
        *,
        state: MeetingState | None = None,
        limit: int = 50,
    ) -> list[MeetingRecord]: ...

    async def clear_meeting_outputs(
        self,
        meeting_id: str,
        *,
        clear_minutes: bool = True,
    ) -> None: ...

    async def set_meeting_rag_projection(
        self,
        meeting_id: str,
        *,
        state: RagProjectionState,
        error: str | None = None,
        projected_at: datetime | None = None,
        retry_backoff: bool = False,
        expected_generation: int | None = None,
    ) -> bool: ...

    async def list_meetings_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[MeetingRecord]: ...

    async def list_meeting_rag_projection_scopes(self) -> list[tuple[str, str, str]]: ...

    async def list_rag_projection_scopes(self) -> list[tuple[str, str, str]]: ...

    # ── Meeting segments ────────────────────────────────────────
    async def append_meeting_segment(
        self,
        meeting_id: str,
        seg: TranscriptSegment,
        *,
        captured_at: datetime,
    ) -> bool:
        """Append only while the meeting is active; return whether it was accepted."""
        ...

    async def append_capture_meeting_segments(
        self,
        meeting_id: str,
        segments: list[TranscriptSegment],
        *,
        captured_at: datetime,
        capture_operation_key: str,
        request_fingerprint: str,
    ) -> CaptureMeetingAppendResult:
        """原子写入或返回同一 capture 已存在的 meeting 段落。"""
        ...

    async def update_capture_meeting_speaker(
        self, *, capture_operation_key: str, request_fingerprint: str,
        speaker_id: str | None, speaker_label: str,
    ) -> list[TranscriptSegment]: ...

    async def list_capture_meeting_segments(
        self,
        meeting_id: str,
        *,
        capture_operation_key: str,
    ) -> list[TranscriptSegment]: ...

    async def get_capture_meeting_id(self, *, capture_operation_key: str) -> str | None: ...

    async def snapshot_meeting_segments_for_finalize(
        self,
        meeting_id: str,
        *,
        ended_at: datetime,
    ) -> list[TranscriptSegment]:
        """Fence future appends and return one complete, stable segment snapshot."""
        ...

    async def settle_capture_requests(
        self,
        meeting_id: str,
        *,
        timeout_s: float,
    ) -> CaptureReceiptSettlement:
        """Wait for accepted capture receipts before closing the append gate."""
        ...

    async def list_meeting_segments(
        self,
        meeting_id: str,
    ) -> list[TranscriptSegment]: ...

    async def count_meeting_segments(self, meeting_id: str) -> int: ...

    async def count_meeting_speakers(self, meeting_id: str) -> int: ...

    # ── Per-meeting speaker label map（与 meeting 内 _speaker_labels 镜像）─
    async def upsert_meeting_speaker_label(
        self,
        meeting_id: str,
        speaker_id: str,
        label: str,
    ) -> None: ...

    async def get_meeting_speaker_labels(
        self,
        meeting_id: str,
    ) -> dict[str, str]: ...

    # ── Ambient segments ────────────────────────────────────────
    async def append_ambient_segment(
        self,
        *,
        audio_ref: str,
        text: str,
        captured_at: datetime,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        duration_ms: int = 0,
        client_segment_id: str | None = None,
    ) -> int: ...

    async def append_capture_ambient_segment(
        self,
        *,
        audio_ref: str,
        text: str,
        captured_at: datetime,
        capture_operation_key: str,
        request_fingerprint: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        duration_ms: int = 0,
        client_segment_id: str | None = None,
    ) -> CaptureAmbientAppendResult:
        """原子写入或返回同一 capture 已存在的 ambient 规范行。"""
        ...

    async def update_ambient_segment_speaker(
        self, segment_id: int, *, speaker_id: str | None, speaker_label: str
    ) -> bool: ...

    async def get_ambient_segment(self, segment_id: int) -> AmbientSegmentRecord | None: ...

    async def list_ambient_segments(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        current_device_only: bool = False,
        limit: int = 100,
    ) -> list[AmbientSegmentRecord]: ...

    async def import_ambient_segments_to_meeting(
        self,
        meeting_id: str,
        *,
        ambient_segment_ids: list[int],
        meeting_started_at: datetime,
    ) -> AmbientMeetingImportResult:
        """Idempotently copy current-device ambient rows into an active meeting."""
        ...

    async def set_ambient_rag_projection(
        self,
        segment_id: int,
        *,
        state: RagProjectionState,
        error: str | None = None,
        projected_at: datetime | None = None,
        retry_backoff: bool = False,
    ) -> bool: ...

    async def list_ambient_segments_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[AmbientSegmentRecord]: ...

    async def count_ambient_segments(self) -> int: ...

    async def register_ambient_audio_file(
        self,
        *,
        audio_ref: str,
        size_bytes: int,
        captured_at: datetime,
        quota_charged: bool,
    ) -> None: ...

    async def list_ambient_audio_files(self) -> list[AmbientAudioFileRecord]: ...

    async def delete_ambient_audio_file(
        self,
        audio_ref: str,
    ) -> AmbientAudioFileRecord | None:
        """Remove inventory row and clear matching ambient segment references."""

        ...

    # ── Capture durable idempotency ─────────────────────────────
    async def claim_capture_request(
        self,
        *,
        operation_key_hash: str,
        client_segment_hash: str,
        idempotency_key_hash: str | None,
        request_fingerprint: str,
        holder_id: str,
        lease_seconds: float,
        meeting_id: str | None = None,
        capture_mode: str | None = None,
    ) -> CaptureRequestClaim: ...

    async def renew_capture_request_claim(
        self,
        claim: CaptureRequestClaim,
        *,
        lease_seconds: float,
    ) -> CaptureRequestClaim | None: ...

    async def release_capture_request_claim(self, claim: CaptureRequestClaim) -> bool: ...

    async def complete_capture_request(
        self,
        claim: CaptureRequestClaim,
        *,
        ambient_stored: bool,
        ambient_segment_id: int | None,
        meeting_id: str | None,
        stt_status: str,
        capture_mode: str,
    ) -> CaptureRequestClaim: ...

    # ── Global speakers registry ────────────────────────────────
    async def upsert_speaker(
        self,
        speaker_id: str,
        *,
        captured_at: datetime,
        label: str | None = None,
        embedding_blob: bytes | None = None,
    ) -> None: ...

    async def get_speaker(self, speaker_id: str) -> SpeakerProfileRecord | None: ...

    async def list_speakers(self) -> list[SpeakerProfileRecord]: ...
