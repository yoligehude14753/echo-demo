"""Focused, no-host-side-effect proof for the B03 capability foundation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agent_capabilities.audit import CapabilityAuditEvent
from app.agent_capabilities.catalog import (
    CAPABILITY_CATALOG,
    catalog,
    evaluate_capability,
    freeze_grant,
)
from app.agent_capabilities.types import (
    CapabilityName,
    CapabilityRequest,
    CommandCapability,
    CommandRequest,
    DenyCode,
    GrantInput,
    InvocationBinding,
    NetworkCapability,
    NetworkRequest,
    PathRequest,
    PermissionRight,
    SkillCapability,
    SkillRequest,
    WorkspaceCapability,
    WorkspaceIdentity,
)
from pydantic import ValidationError

NOW = datetime(2030, 1, 1, tzinfo=UTC)
IDENTITY = WorkspaceIdentity(workspace_id="ws-1", identity="root-identity-1")


def _grant(*, expires_at: datetime = NOW + timedelta(hours=1)):
    return freeze_grant(
        GrantInput(
            grant_id="grant-1",
            revision=7,
            policy_revision=11,
            task_id="task-1",
            operation_key="op-1",
            workspace_identity=IDENTITY,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=expires_at,
            workspace_roots=(
                WorkspaceCapability(
                    root_id="root-1",
                    canonical_path="/workspace/project",
                    identity="root-identity-1",
                    rights=(PermissionRight.READ, PermissionRight.WRITE, PermissionRight.DELETE),
                ),
            ),
            command=CommandCapability(
                mode="explicit",
                allowed_executables=("python3",),
                allowed_env_names=("LANG",),
                max_wall_seconds=20,
                max_output_bytes=4096,
            ),
            network=NetworkCapability(
                mode="allowlist",
                hosts=("api.example.com",),
                schemes=("https",),
                ports=(443,),
            ),
            skills=SkillCapability(
                mode="allowlist", identities=("bundled.summarize",), versions=("1.0.0",)
            ),
        )
    )


def _binding() -> InvocationBinding:
    return InvocationBinding(
        task_id="task-1",
        operation_key="op-1",
        workspace_identity=IDENTITY,
        policy_revision=11,
    )


def _request(capability: CapabilityName, **kwargs: object) -> CapabilityRequest:
    return CapabilityRequest(capability=capability, binding=_binding(), **kwargs)


def test_snapshot_is_deeply_immutable_and_contains_binding_fields() -> None:
    snapshot = _grant()

    assert snapshot.schema_version == 1
    assert (snapshot.task_id, snapshot.operation_key) == ("task-1", "op-1")
    assert snapshot.workspace_identity == IDENTITY
    assert snapshot.policy_revision == 11
    with pytest.raises(ValidationError):
        snapshot.task_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.workspace_roots[0].rights += (PermissionRight.CREATE,)  # type: ignore[misc]
    assert isinstance(snapshot.workspace_roots, tuple)
    assert isinstance(snapshot.command.allowed_executables, tuple)


def test_binding_expiry_and_stale_revision_fail_closed() -> None:
    snapshot = _grant()
    base = _request(
        CapabilityName.SKILL_USE,
        skill=SkillRequest(identity="bundled.summarize", version="1.0.0"),
    )
    assert evaluate_capability(snapshot, base, now=NOW).code is DenyCode.ALLOWED

    wrong_operation = base.model_copy(
        update={"binding": _binding().model_copy(update={"operation_key": "other"})}
    )
    assert (
        evaluate_capability(snapshot, wrong_operation, now=NOW).code
        is DenyCode.GRANT_BINDING_MISMATCH
    )
    assert (
        evaluate_capability(snapshot, base, now=NOW, active_policy_revision=12).code
        is DenyCode.GRANT_STALE
    )
    assert evaluate_capability(_grant(expires_at=NOW), base, now=NOW).code is DenyCode.GRANT_EXPIRED


def test_catalog_is_frozen_and_unknown_capability_is_denied() -> None:
    assert len(catalog()) == len(CAPABILITY_CATALOG) == 8
    with pytest.raises(TypeError):
        CAPABILITY_CATALOG[CapabilityName.PATH_READ] = CAPABILITY_CATALOG[CapabilityName.PATH_READ]  # type: ignore[index]
    unknown = CapabilityRequest(capability="path.read.unknown", binding=_binding())
    decision = evaluate_capability(_grant(), unknown, now=NOW)
    assert decision.outcome.value == "deny"
    assert decision.code is DenyCode.CAPABILITY_UNKNOWN


def test_path_ambiguity_matrix_is_pure_and_host_boundary_is_explicit() -> None:
    inside = _request(
        CapabilityName.PATH_READ,
        path=PathRequest(
            path="/workspace/project/notes.txt", root_id="root-1", right=PermissionRight.READ
        ),
    )
    assert (
        evaluate_capability(_grant(), inside, now=NOW).code is DenyCode.HOST_VERIFICATION_REQUIRED
    )

    verified = inside.model_copy(
        update={
            "path": inside.path.model_copy(
                update={"host_verified": True, "observed_identity": "root-identity-1"}
            )
        }
    )
    assert evaluate_capability(_grant(), verified, now=NOW).code is DenyCode.ALLOWED

    ambiguous = inside.model_copy(
        update={"path": inside.path.model_copy(update={"path": "/workspace/project/../secret.txt"})}
    )
    assert evaluate_capability(_grant(), ambiguous, now=NOW).code is DenyCode.TOOL_PATH_AMBIGUOUS

    outside = inside.model_copy(
        update={"path": inside.path.model_copy(update={"path": "/tmp/secret.txt"})}
    )
    assert (
        evaluate_capability(_grant(), outside, now=NOW).code is DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE
    )


def test_command_matrix_uses_argv_and_never_shell_or_raw_env_values() -> None:
    allowed = _request(
        CapabilityName.COMMAND_EXECUTE,
        command=CommandRequest(
            argv=("python3", "-c", "print('literal;')"),
            cwd="/workspace/project",
            env_names=("LANG",),
            executable_identity_verified=True,
        ),
    )
    assert evaluate_capability(_grant(), allowed, now=NOW).code is DenyCode.ALLOWED

    shell = allowed.model_copy(
        update={"command": allowed.command.model_copy(update={"shell": True})}
    )
    assert evaluate_capability(_grant(), shell, now=NOW).code is DenyCode.TOOL_COMMAND_DENIED
    env_injection = allowed.model_copy(
        update={
            "command": allowed.command.model_copy(update={"env_names": ("LANG", "SECRET_VALUE")})
        }
    )
    assert (
        evaluate_capability(_grant(), env_injection, now=NOW).code is DenyCode.TOOL_COMMAND_DENIED
    )


def test_network_matrix_denies_private_and_requires_dns_host_verification() -> None:
    unresolved = _request(
        CapabilityName.NETWORK_CONNECT,
        network=NetworkRequest(scheme="https", host="api.example.com", port=443),
    )
    assert (
        evaluate_capability(_grant(), unresolved, now=NOW).code
        is DenyCode.HOST_VERIFICATION_REQUIRED
    )

    public = unresolved.model_copy(
        update={
            "network": unresolved.network.model_copy(update={"resolved_addresses": ("8.8.8.8",)})
        }
    )
    assert evaluate_capability(_grant(), public, now=NOW).code is DenyCode.ALLOWED

    private = unresolved.model_copy(
        update={
            "network": unresolved.network.model_copy(update={"resolved_addresses": ("127.0.0.1",)})
        }
    )
    assert evaluate_capability(_grant(), private, now=NOW).code is DenyCode.TOOL_NETWORK_DENIED


def test_skill_allowlist_and_audit_schema_never_store_secret_values() -> None:
    decision = evaluate_capability(
        _grant(),
        _request(
            CapabilityName.SKILL_USE,
            skill=SkillRequest(identity="bundled.summarize", version="1.0.0"),
        ),
        now=NOW,
    )
    event = CapabilityAuditEvent.from_decision(decision, event_id="audit-1", occurred_at=NOW)
    payload = event.model_dump_json()
    assert event.code is DenyCode.ALLOWED
    assert "secret-value" not in payload
    assert "token-value" not in payload
    assert set(event.model_dump()) == {
        "schema_version",
        "event_type",
        "event_id",
        "occurred_at",
        "outcome",
        "code",
        "capability",
        "task_id",
        "operation_key",
        "grant_id",
        "grant_revision",
        "policy_revision",
        "workspace_id",
        "workspace_identity",
        "host_verification_required",
    }
    with pytest.raises(ValidationError):
        CapabilityAuditEvent.model_validate({**event.model_dump(), "secret_value": "secret-value"})
