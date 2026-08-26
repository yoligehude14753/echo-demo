"""Shared fail-closed context and receipt primitives for B06P file hosts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import Field, field_validator

from ..catalog import evaluate_capability
from ..redaction import redact_audit_event
from ..types import (
    CapabilityDecision,
    CapabilityName,
    CapabilityRequest,
    DecisionOutcome,
    DenyCode,
    FrozenModel,
    GrantSnapshot,
    InvocationBinding,
    PathRequest,
    PermissionRight,
    WorkspaceIdentity,
)

SnapshotProvider = Callable[[], GrantSnapshot | None]
CancelChecker = Callable[[], bool]
T = TypeVar("T")


def _identifier(value: str) -> str:
    if not value or any(char in value for char in "\x00\r\n"):
        raise ValueError("identifier must be non-empty and control-character free")
    return value


class ToolInvocation(FrozenModel):
    """The host-owned binding that adds tool correlation to B03's binding."""

    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    tool_use_id: str = Field(min_length=1, max_length=256, alias="toolUseId")
    grant_revision: int = Field(ge=1, alias="grantRevision")
    policy_revision: int = Field(ge=1, alias="policyRevision")
    workspace_identity: WorkspaceIdentity

    _validate_ids = field_validator("task_id", "operation_key", "tool_use_id")(_identifier)

    def to_binding(self) -> InvocationBinding:
        return InvocationBinding(
            task_id=self.task_id,
            operation_key=self.operation_key,
            workspace_identity=self.workspace_identity,
            policy_revision=self.policy_revision,
        )


class OperationReceipt(FrozenModel):
    """Value-free receipt emitted for every host attempt, including denies."""

    schema_version: Literal[1] = 1
    event_type: Literal["capability.operation.receipt"] = "capability.operation.receipt"
    receipt_id: str = Field(min_length=1, max_length=256, alias="receiptId")
    occurred_at: datetime = Field(alias="occurredAt")
    operation: str = Field(min_length=1, max_length=128)
    outcome: Literal["allow", "deny"]
    result: Literal["succeeded", "denied", "failed"]
    code: DenyCode
    capability: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=256, alias="taskId")
    operation_key: str = Field(min_length=1, max_length=256, alias="operationKey")
    tool_use_id: str = Field(min_length=1, max_length=256, alias="toolUseId")
    grant_id: str | None = Field(default=None, max_length=256, alias="grantId")
    grant_revision: int | None = Field(default=None, ge=1, alias="grantRevision")
    policy_revision: int | None = Field(default=None, ge=1, alias="policyRevision")
    workspace_id: str = Field(min_length=1, max_length=256, alias="workspaceId")
    metadata: tuple[tuple[str, str], ...] = ()
    redacted: bool = True

    @field_validator("occurred_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class HostResult(Generic[T]):
    """A host value plus its policy decision and receipt."""

    value: T | None
    decision: CapabilityDecision
    receipt: OperationReceipt
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.receipt.result == "succeeded"


@dataclass(frozen=True)
class HostContext:
    """Execution controls required before and after every host side effect."""

    grant: GrantSnapshot
    invocation: ToolInvocation
    current_grant: SnapshotProvider | None
    is_cancelled: CancelChecker | None
    active_policy_revision: int | None = None
    now: datetime | None = None

    def _control_decision(self, capability: str) -> CapabilityDecision | None:  # noqa: PLR0911
        current = self.current_grant() if self.current_grant is not None else None
        if current is None:
            return _decision(self, capability, DenyCode.GRANT_REVOKED)
        if self.is_cancelled is not None and self.is_cancelled():
            return _decision(self, capability, DenyCode.GRANT_REVOKED)
        if (
            current.grant_id != self.grant.grant_id
            or current.task_id != self.grant.task_id
            or current.operation_key != self.grant.operation_key
            or current.workspace_identity != self.grant.workspace_identity
        ):
            return _decision(self, capability, DenyCode.GRANT_BINDING_MISMATCH)
        if self.invocation.grant_revision != current.revision:
            return _decision(self, capability, DenyCode.GRANT_REVISION_MISMATCH)
        if self.invocation.policy_revision != current.policy_revision:
            return _decision(self, capability, DenyCode.GRANT_REVISION_MISMATCH)
        if (
            self.active_policy_revision is not None
            and current.policy_revision != self.active_policy_revision
        ):
            return _decision(self, capability, DenyCode.GRANT_STALE)
        return None

    def authorize(self, request: CapabilityRequest) -> CapabilityDecision:
        """Recheck task controls, then delegate the decision to B03 pure policy."""

        capability = (
            request.capability.value
            if isinstance(request.capability, CapabilityName)
            else request.capability
        )
        control_failure = self._control_decision(capability)
        if control_failure is not None:
            return control_failure
        if request.binding != self.invocation.to_binding():
            return _decision(self, capability, DenyCode.GRANT_BINDING_MISMATCH)
        return evaluate_capability(
            self.grant,
            request,
            now=self.now or datetime.now(UTC),
            active_policy_revision=self.active_policy_revision,
        )

    def path_request(
        self,
        *,
        capability: CapabilityName,
        path: str,
        root_id: str,
        right: PermissionRight,
        host_verified: bool,
        observed_identity: str | None,
    ) -> CapabilityRequest:
        return CapabilityRequest(
            capability=capability,
            binding=self.invocation.to_binding(),
            path=PathRequest(
                path=path,
                root_id=root_id,
                right=right,
                host_verified=host_verified,
                observed_identity=observed_identity,
            ),
        )


def _decision(context: HostContext, capability: str, code: DenyCode) -> CapabilityDecision:
    return CapabilityDecision(
        outcome=DecisionOutcome.DENY,
        code=code,
        capability=capability,
        task_id=context.invocation.task_id,
        operation_key=context.invocation.operation_key,
        workspace_identity=context.invocation.workspace_identity,
        grant_id=context.grant.grant_id,
        grant_revision=context.grant.revision,
        policy_revision=context.grant.policy_revision,
    )


def _metadata(values: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Redact first, then serialize stable scalar metadata only."""

    safe = redact_audit_event(values)
    return tuple(
        (str(key), json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        for key, value in sorted(safe.items(), key=lambda item: str(item[0]))
    )


def target_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def receipt_for(
    context: HostContext,
    *,
    operation: str,
    decision: CapabilityDecision,
    result: Literal["succeeded", "denied", "failed"],
    metadata: Mapping[str, Any] = {},
) -> OperationReceipt:
    occurred_at = context.now or datetime.now(UTC)
    identity = (
        f"{operation}:{context.invocation.task_id}:{context.invocation.operation_key}:"
        f"{context.invocation.tool_use_id}:{context.invocation.grant_revision}:{occurred_at.isoformat()}"
    )
    receipt_id = "receipt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return OperationReceipt(
        receiptId=receipt_id,
        occurredAt=occurred_at,
        operation=operation,
        outcome="allow" if decision.allowed else "deny",
        result=result,
        code=decision.code,
        capability=decision.capability,
        taskId=context.invocation.task_id,
        operationKey=context.invocation.operation_key,
        toolUseId=context.invocation.tool_use_id,
        grantId=decision.grant_id,
        grantRevision=decision.grant_revision,
        policyRevision=decision.policy_revision,
        workspaceId=context.invocation.workspace_identity.workspace_id,
        metadata=_metadata(metadata),
    )


def denied(
    context: HostContext,
    *,
    operation: str,
    capability: str,
    code: DenyCode,
    metadata: Mapping[str, Any] = {},
) -> HostResult[Any]:
    decision = _decision(context, capability, code)
    return HostResult(
        None,
        decision,
        receipt_for(
            context, operation=operation, decision=decision, result="denied", metadata=metadata
        ),
    )


def failed(
    context: HostContext,
    *,
    operation: str,
    decision: CapabilityDecision,
    error_code: str,
    metadata: Mapping[str, Any] = {},
) -> HostResult[Any]:
    return HostResult(
        None,
        decision,
        receipt_for(
            context, operation=operation, decision=decision, result="failed", metadata=metadata
        ),
        error_code=error_code,
    )


def succeeded(
    context: HostContext,
    *,
    operation: str,
    decision: CapabilityDecision,
    value: T,
    metadata: Mapping[str, Any] = {},
) -> HostResult[T]:
    return HostResult(
        value,
        decision,
        receipt_for(
            context, operation=operation, decision=decision, result="succeeded", metadata=metadata
        ),
    )


__all__ = [
    "HostContext",
    "HostResult",
    "OperationReceipt",
    "ToolInvocation",
    "denied",
    "failed",
    "receipt_for",
    "succeeded",
    "target_digest",
]
