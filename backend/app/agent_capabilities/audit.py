"""Stable, value-free audit records for pure capability decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from .types import CapabilityDecision, DenyCode, FrozenModel


def _audit_non_blank(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("audit identifier must be non-empty and value-free")
    return value


class CapabilityAuditEvent(FrozenModel):
    """Audit schema containing identifiers and codes, never target/secret values."""

    schema_version: Literal[1] = 1
    event_type: Literal["capability.decision"] = "capability.decision"
    event_id: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    outcome: Literal["allow", "deny"]
    code: DenyCode
    capability: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    grant_id: str | None = Field(default=None, max_length=256)
    grant_revision: int | None = Field(default=None, ge=1)
    policy_revision: int | None = Field(default=None, ge=1)
    workspace_id: str = Field(min_length=1, max_length=256)
    workspace_identity: str = Field(min_length=1, max_length=256)
    host_verification_required: bool = False

    _validate_ids = field_validator(
        "event_id", "capability", "task_id", "operation_key", "workspace_id", "workspace_identity"
    )(_audit_non_blank)

    @field_validator("occurred_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value.astimezone(UTC)

    @classmethod
    def from_decision(
        cls,
        decision: CapabilityDecision,
        *,
        event_id: str,
        occurred_at: datetime,
    ) -> CapabilityAuditEvent:
        return cls(
            event_id=event_id,
            occurred_at=occurred_at,
            outcome=decision.outcome.value,
            code=decision.code,
            capability=decision.capability,
            task_id=decision.task_id,
            operation_key=decision.operation_key,
            grant_id=decision.grant_id,
            grant_revision=decision.grant_revision,
            policy_revision=decision.policy_revision,
            workspace_id=decision.workspace_identity.workspace_id,
            workspace_identity=decision.workspace_identity.identity,
            host_verification_required=decision.host_verification_required,
        )


AuditEvent = CapabilityAuditEvent


def audit_event_from_decision(
    decision: CapabilityDecision,
    *,
    event_id: str,
    occurred_at: datetime,
) -> CapabilityAuditEvent:
    return CapabilityAuditEvent.from_decision(
        decision,
        event_id=event_id,
        occurred_at=occurred_at,
    )


__all__ = ["AuditEvent", "CapabilityAuditEvent", "audit_event_from_decision"]
