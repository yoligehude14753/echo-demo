"""Memory domain values shared by the service, repository and HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.security.models import Principal

MemoryLevel = Literal["L0", "L1", "L2", "L3"]
MemoryKind = Literal["fact", "preference", "decision", "todo", "relationship"]
MemoryStatus = Literal["active", "superseded", "deleted"]
MemorySourceKind = Literal[
    "conversation_user",
    "conversation_assistant",
    "meeting_segment",
    "meeting_minutes",
    "ambient_segment",
    "artifact",
    "user_explicit",
    "legacy_import",
]


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Server-authored isolation scope captured before background work starts."""

    tenant_id: str
    owner_id: str
    device_id: str
    session_id: str

    @classmethod
    def from_principal(cls, principal: Principal) -> MemoryScope:
        return cls(
            tenant_id=principal.tenant_id,
            owner_id=principal.owner_id,
            device_id=principal.device_id,
            session_id=principal.session_id,
        )


class ProvenanceInput(BaseModel):
    source_kind: MemorySourceKind
    source_id: str = Field(min_length=1, max_length=512)
    source_segment_id: str | None = Field(default=None, max_length=512)
    meeting_id: str | None = Field(default=None, max_length=512)
    artifact_id: str | None = Field(default=None, max_length=512)
    excerpt: str = Field(min_length=1, max_length=8_000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryWriteCandidate(BaseModel):
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=2_000)
    canonical_key: str = Field(min_length=1, max_length=256)
    subject: str | None = Field(default=None, max_length=256)
    confidence: float = Field(ge=0.0, le=1.0)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    scope: str = Field(default="owner", min_length=1, max_length=128)
    action: Literal["add", "reaffirm", "supersede", "ignore"] = "add"
    existing_memory_id: str | None = Field(default=None, max_length=128)
    evidence_quote: str = Field(min_length=1, max_length=2_000)
    relation_memory_ids: list[str] = Field(default_factory=list, max_length=20)


class MemoryRecord(BaseModel):
    memory_id: str
    kind: MemoryKind
    content: str
    canonical_key: str
    subject: str | None = None
    confidence: float
    salience: float
    scope: str
    status: MemoryStatus
    hit_count: int
    source_count: int
    user_confirmed: bool
    created_at: datetime
    last_seen_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    deleted_at: datetime | None = None
    revision: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvenanceRecord(BaseModel):
    provenance_id: str
    memory_id: str
    source_kind: MemorySourceKind
    source_id: str
    source_segment_id: str | None = None
    meeting_id: str | None = None
    artifact_id: str | None = None
    excerpt: str
    confidence: float
    occurred_at: datetime
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProfileSettingRecord(BaseModel):
    config_key: str
    value: Any
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime
    deleted_at: datetime | None = None
    revision: int


class RecallCandidate(BaseModel):
    candidate_id: str
    level: MemoryLevel
    content: str
    source_ref: str
    occurred_at: datetime
    salience: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    kind: str
    memory_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    deterministic_score: float = 0.0


class RecallMatch(BaseModel):
    candidate: RecallCandidate
    relevance: float = Field(ge=0.0, le=1.0)
    relation: str = Field(default="相关", max_length=300)
    score: float = Field(ge=0.0)


class RecallResult(BaseModel):
    query: str
    matches: list[RecallMatch] = Field(default_factory=list)
    used_small_model: bool = False
    small_model: str | None = None
    latency_ms: float = Field(default=0.0, ge=0.0)

    def prompt_context(self) -> str:
        if not self.matches:
            return ""
        lines = [
            "以下是与当前问题匹配的 EchoDesk memory。只可使用列出的原文事实，"
            "不得把‘关联原因’扩写成新事实；使用 L2/L3 内容时保留引用标记。"
        ]
        for match in self.matches:
            item = match.candidate
            citation = item.source_ref
            if item.memory_id:
                citation = f"memory:{item.memory_id}; {citation}"
            lines.append(
                f"- [{item.level}] {item.content}（关联：{match.relation}；来源：{citation}）"
            )
        return "\n".join(lines)


class ExtractionResult(BaseModel):
    run_id: str
    source_id: str
    state: Literal["succeeded", "failed", "skipped"]
    memories: list[MemoryRecord] = Field(default_factory=list)
    rejected_count: int = 0
    model: str
    model_display_name: str
    latency_ms: float = Field(default=0.0, ge=0.0)
    error: str | None = None


__all__ = [
    "ExtractionResult",
    "MemoryKind",
    "MemoryLevel",
    "MemoryRecord",
    "MemoryScope",
    "MemorySourceKind",
    "MemoryStatus",
    "MemoryWriteCandidate",
    "ProfileSettingRecord",
    "ProvenanceInput",
    "ProvenanceRecord",
    "RecallCandidate",
    "RecallMatch",
    "RecallResult",
]
