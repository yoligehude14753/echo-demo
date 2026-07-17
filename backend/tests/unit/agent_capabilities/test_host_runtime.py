"""B06P main-task contract tests for the shared host boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agent_capabilities import (
    CancellationToken,
    CancelReason,
    CapabilityHostRegistry,
    CapabilityInvocation,
    CapabilityName,
    CapabilityRequest,
    GrantInput,
    HostOutcome,
    InvocationBinding,
    OperationReceipt,
    PathRequest,
    WorkspaceCapability,
    WorkspaceIdentity,
    freeze_grant,
    make_receipt,
)
from app.agent_capabilities.types import (
    CapabilityDecision,
    DecisionOutcome,
    DenyCode,
    PermissionRight,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
IDENTITY = WorkspaceIdentity(workspace_id="ws-1", identity="root-1")


def _grant(*, task_id: str = "task-1", operation_key: str = "op-1", revision: int = 7):
    return freeze_grant(
        GrantInput(
            grant_id="grant-1",
            revision=revision,
            policy_revision=11,
            task_id=task_id,
            operation_key=operation_key,
            workspace_identity=IDENTITY,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            workspace_roots=(
                WorkspaceCapability(
                    root_id="root-1",
                    canonical_path="/workspace/project",
                    identity="root-1",
                    rights=(PermissionRight.READ,),
                ),
            ),
        )
    )


def _invocation(
    grant,
    *,
    tool_use_id: str = "tool-1",
    grant_revision: int | None = None,
    binding_task_id: str | None = None,
    binding_identity: WorkspaceIdentity | None = None,
    binding_policy_revision: int | None = None,
):
    return CapabilityInvocation(
        grant=grant,
        request=CapabilityRequest(
            capability=CapabilityName.PATH_READ,
            binding=InvocationBinding(
                task_id=binding_task_id or grant.task_id,
                operation_key=grant.operation_key,
                workspace_identity=binding_identity or IDENTITY,
                policy_revision=binding_policy_revision or grant.policy_revision,
            ),
            path=PathRequest(
                path="/workspace/project/readme.txt",
                root_id="root-1",
                right=PermissionRight.READ,
                host_verified=True,
                observed_identity="root-1",
            ),
        ),
        toolUseId=tool_use_id,
        grantRevision=grant_revision or grant.revision,
    )


def _allowed(invocation: CapabilityInvocation, _cancel: CancellationToken) -> HostOutcome:
    decision = CapabilityDecision(
        outcome=DecisionOutcome.ALLOW,
        code=DenyCode.ALLOWED,
        capability=invocation.capability,
        task_id=invocation.task_id,
        operation_key=invocation.operation_key,
        workspace_identity=invocation.workspace_identity,
        grant_id=invocation.grant.grant_id,
        grant_revision=invocation.grant.revision,
        policy_revision=invocation.grant.policy_revision,
    )
    return HostOutcome(
        {"ok": True},
        decision,
        make_receipt(
            invocation,
            operation="test.host",
            decision=decision,
            result="succeeded",
            metadata={"secret": "must-not-appear", "target_digest": "safe-digest"},
            occurred_at=NOW,
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "invocation", "code"),
    (
        ("missing", _invocation(_grant()), DenyCode.TOOL_NOT_REGISTERED),
        ("host", _invocation(_grant(), grant_revision=8), DenyCode.GRANT_REVISION_MISMATCH),
        (
            "host",
            _invocation(_grant(), binding_task_id="other"),
            DenyCode.GRANT_BINDING_MISMATCH,
        ),
        (
            "host",
            _invocation(_grant(), binding_identity=WorkspaceIdentity(workspace_id="other", identity="root-1")),
            DenyCode.GRANT_BINDING_MISMATCH,
        ),
        ("host", _invocation(_grant(), binding_policy_revision=12), DenyCode.GRANT_REVISION_MISMATCH),
    ),
)
def test_registry_rejects_unknown_or_mismatched_invocations(name, invocation, code) -> None:
    registry = CapabilityHostRegistry()
    if name == "host":
        registry.register(name, _allowed)
    result = registry.invoke(name, invocation)
    assert not result.ok
    assert result.decision.code is code
    assert result.receipt.result == "denied"
    assert result.receipt.tool_use_id == invocation.tool_use_id


@pytest.mark.unit
def test_shared_receipt_is_redacted_and_cancellation_is_idempotent() -> None:
    grant = _grant()
    invocation = _invocation(grant)
    registry = CapabilityHostRegistry()
    seen: list[CancelReason | None] = []

    def handler(current: CapabilityInvocation, token: CancellationToken) -> HostOutcome:
        assert registry.cancel(current.tool_use_id, CancelReason.REVOKED)
        assert not registry.cancel(current.tool_use_id, CancelReason.TIMEOUT)
        seen.append(token.reason)
        decision = CapabilityDecision(
            outcome=DecisionOutcome.DENY,
            code=DenyCode.GRANT_REVOKED,
            capability=current.capability,
            task_id=current.task_id,
            operation_key=current.operation_key,
            workspace_identity=current.workspace_identity,
            grant_id=current.grant.grant_id,
            grant_revision=current.grant.revision,
            policy_revision=current.grant.policy_revision,
        )
        return HostOutcome(
            None,
            decision,
            make_receipt(current, operation="test.host", decision=decision, result="denied", occurred_at=NOW),
        )

    registry.register("host", handler)
    result = registry.invoke("host", invocation)
    assert not result.ok
    assert seen == [CancelReason.REVOKED]
    rendered = result.receipt.model_dump_json(by_alias=True)
    assert "must-not-appear" not in rendered
    assert "secret" not in rendered
    assert result.receipt.redacted


@pytest.mark.unit
def test_registry_rejects_duplicate_registration_and_exports_frozen_receipt() -> None:
    registry = CapabilityHostRegistry()
    registry.register("host", _allowed)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("host", _allowed)
    receipt = OperationReceipt.model_validate(
        registry.invoke("unknown", _invocation(_grant())).receipt.model_dump(by_alias=True)
    )
    assert receipt.schema_version == 1
