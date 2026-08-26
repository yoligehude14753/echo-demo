"""Shared invocation, cancellation, receipt, and registry boundary for B06P.

The registry owns orchestration only.  B03 remains the authority source and
each concrete host remains responsible for its final host verification.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import Field, field_validator

from .redaction import redact_audit_event
from .types import (
    CapabilityDecision,
    CapabilityName,
    CapabilityRequest,
    DecisionOutcome,
    DenyCode,
    FrozenModel,
    GrantSnapshot,
    WorkspaceIdentity,
)

UNSUPPORTED_P0_FAIL_CLOSED: Final[str] = "UNSUPPORTED_P0_FAIL_CLOSED"


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\x00\r\n"):
        raise ValueError("identifier must be non-empty and control-character free")
    return value


class CancelReason(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    REVOKED = "revoked"
    CLOSED = "closed"


class CancellationToken:
    """Thread-safe, idempotent cancellation state shared by one invocation."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: CancelReason | None = None

    def cancel(self, reason: CancelReason = CancelReason.CANCELLED) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> CancelReason | None:
        with self._lock:
            return self._reason


class CapabilityInvocation(FrozenModel):
    """Immutable common envelope consumed by every concrete host."""

    grant: GrantSnapshot
    request: CapabilityRequest
    tool_use_id: str = Field(min_length=1, max_length=256, alias="toolUseId")
    grant_revision: int = Field(ge=1, alias="grantRevision")

    _validate_tool_use_id = field_validator("tool_use_id")(_identifier)

    @property
    def task_id(self) -> str:
        return self.request.binding.task_id

    @property
    def operation_key(self) -> str:
        return self.request.binding.operation_key

    @property
    def workspace_identity(self) -> WorkspaceIdentity:
        return self.request.binding.workspace_identity

    @property
    def capability(self) -> str:
        value = self.request.capability
        return value.value if isinstance(value, CapabilityName) else value


class OperationReceipt(FrozenModel):
    """Value-free receipt shared by registry-level denies and host outcomes."""

    schema_version: int = 1
    event_type: str = "capability.operation.receipt"
    receipt_id: str = Field(min_length=1, max_length=256, alias="receiptId")
    occurred_at: datetime = Field(alias="occurredAt")
    operation: str = Field(min_length=1, max_length=128)
    outcome: str
    result: str
    code: str
    capability: str
    task_id: str = Field(alias="taskId")
    operation_key: str = Field(alias="operationKey")
    tool_use_id: str = Field(alias="toolUseId")
    grant_id: str | None = Field(default=None, alias="grantId")
    grant_revision: int | None = Field(default=None, alias="grantRevision")
    policy_revision: int | None = Field(default=None, alias="policyRevision")
    workspace_id: str = Field(alias="workspaceId")
    metadata: tuple[tuple[str, str], ...] = ()
    redacted: bool = True

    @field_validator("occurred_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class HostOutcome:
    value: Any
    decision: CapabilityDecision
    receipt: OperationReceipt

    @property
    def ok(self) -> bool:
        return self.receipt.result == "succeeded" and self.decision.allowed


HostHandler = Callable[[CapabilityInvocation, CancellationToken], HostOutcome]


def make_receipt(
    invocation: CapabilityInvocation,
    *,
    operation: str,
    decision: CapabilityDecision,
    result: str,
    code: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> OperationReceipt:
    """Create a deterministic-shape, redacted receipt for every attempt."""

    safe = redact_audit_event(dict(metadata or {}))
    serialized = tuple(
        (str(key), str(value)) for key, value in sorted(safe.items(), key=lambda item: str(item[0]))
    )
    return OperationReceipt.model_validate(
        {
            "receiptId": f"receipt_{uuid.uuid4().hex}",
            "occurredAt": (occurred_at or datetime.now(UTC)).astimezone(UTC),
            "operation": operation,
            "outcome": "allow" if decision.allowed and result == "succeeded" else "deny",
            "result": result,
            "code": code or decision.code.value,
            "capability": decision.capability,
            "taskId": decision.task_id,
            "operationKey": decision.operation_key,
            "toolUseId": invocation.tool_use_id,
            "grantId": decision.grant_id,
            "grantRevision": decision.grant_revision,
            "policyRevision": decision.policy_revision,
            "workspaceId": decision.workspace_identity.workspace_id,
            "metadata": serialized,
        }
    )


def _registry_decision(invocation: CapabilityInvocation, code: DenyCode) -> CapabilityDecision:
    return CapabilityDecision(
        outcome=DecisionOutcome.DENY,
        code=code,
        capability=invocation.capability,
        task_id=invocation.task_id,
        operation_key=invocation.operation_key,
        workspace_identity=invocation.workspace_identity,
        grant_id=invocation.grant.grant_id,
        grant_revision=invocation.grant.revision,
        policy_revision=invocation.grant.policy_revision,
    )


class CapabilityHostRegistry:
    """Small explicit registry; it never infers hosts from environment state."""

    def __init__(self) -> None:
        self._handlers: dict[str, HostHandler] = {}
        self._active: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def register(self, name: str, handler: HostHandler) -> None:
        name = _identifier(name)
        with self._lock:
            if name in self._handlers:
                raise ValueError(f"host already registered: {name}")
            self._handlers[name] = handler

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._handlers.pop(name, None) is not None

    def cancel(self, tool_use_id: str, reason: CancelReason = CancelReason.CANCELLED) -> bool:
        with self._lock:
            token = self._active.get(tool_use_id)
        return token.cancel(reason) if token is not None else False

    def revoke_all(self) -> int:
        with self._lock:
            tokens = tuple(self._active.values())
        return sum(token.cancel(CancelReason.REVOKED) for token in tokens)

    def invoke(self, name: str, invocation: CapabilityInvocation) -> HostOutcome:
        with self._lock:
            handler = self._handlers.get(name)
            if handler is None:
                decision = _registry_decision(invocation, DenyCode.TOOL_NOT_REGISTERED)
                return HostOutcome(
                    None,
                    decision,
                    make_receipt(invocation, operation=name, decision=decision, result="denied"),
                )
            if invocation.grant_revision != invocation.grant.revision:
                decision = _registry_decision(invocation, DenyCode.GRANT_REVISION_MISMATCH)
                return HostOutcome(
                    None,
                    decision,
                    make_receipt(invocation, operation=name, decision=decision, result="denied"),
                )
            if (
                invocation.request.binding.task_id != invocation.grant.task_id
                or invocation.request.binding.operation_key != invocation.grant.operation_key
                or invocation.request.binding.workspace_identity
                != invocation.grant.workspace_identity
            ):
                decision = _registry_decision(invocation, DenyCode.GRANT_BINDING_MISMATCH)
                return HostOutcome(
                    None,
                    decision,
                    make_receipt(invocation, operation=name, decision=decision, result="denied"),
                )
            if invocation.request.binding.policy_revision != invocation.grant.policy_revision:
                decision = _registry_decision(invocation, DenyCode.GRANT_REVISION_MISMATCH)
                return HostOutcome(
                    None,
                    decision,
                    make_receipt(invocation, operation=name, decision=decision, result="denied"),
                )
            token = CancellationToken()
            self._active[invocation.tool_use_id] = token
        try:
            if token.is_cancelled():
                decision = _registry_decision(invocation, DenyCode.GRANT_REVOKED)
                return HostOutcome(
                    None,
                    decision,
                    make_receipt(invocation, operation=name, decision=decision, result="denied"),
                )
            return handler(invocation, token)
        finally:
            with self._lock:
                self._active.pop(invocation.tool_use_id, None)


__all__ = [
    "UNSUPPORTED_P0_FAIL_CLOSED",
    "CancelReason",
    "CancellationToken",
    "CapabilityHostRegistry",
    "CapabilityInvocation",
    "HostOutcome",
    "OperationReceipt",
    "make_receipt",
]
