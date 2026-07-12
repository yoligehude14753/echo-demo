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
MinutesStatus = Literal["generating", "ok", "generation_failed"]
RagProjectionState = Literal[
    "index_pending",
    "indexed",
    "index_failed",
    "delete_pending",
    "deleted",
    "delete_failed",
]


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


class AmbientSegmentRecord(BaseModel):
    """ambient_segments 单行。"""

    id: int = 0
    audio_ref: str
    text: str
    speaker_id: str | None = None
    speaker_label: str | None = None
    duration_ms: int = 0
    captured_at: datetime


class AmbientAudioFileRecord(BaseModel):
    """Owner-scoped ambient WAV inventory row (migration 027)."""

    audio_ref: str
    size_bytes: int
    captured_at: datetime
    quota_charged: bool = False


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
    ) -> MeetingRecord:
        """Create or return the principal's authoritative active meeting."""
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
    ) -> None: ...

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
    ) -> None: ...

    async def list_meetings_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[MeetingRecord]: ...

    async def list_meeting_rag_projection_scopes(self) -> list[tuple[str, str, str]]: ...

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

    async def snapshot_meeting_segments_for_finalize(
        self,
        meeting_id: str,
        *,
        ended_at: datetime,
    ) -> list[TranscriptSegment]:
        """Fence future appends and return one complete, stable segment snapshot."""
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
    ) -> int: ...

    async def list_ambient_segments(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
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
