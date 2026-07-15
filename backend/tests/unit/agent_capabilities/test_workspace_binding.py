"""Narrow B06P rework proof for binding B03 grants to host root evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.agent_capabilities import (
    GrantSnapshot,
    VerifiedWorkspaceBinding,
    VerifiedWorkspaceRoot,
    bind_verified_workspace,
    compile_grant,
)
from app.agent_capabilities.hosts import FileReadHost, HostContext, PathVerifier, ToolInvocation
from app.agent_capabilities.policy import CapabilityFact, PermissionFacts
from app.agent_capabilities.types import DenyCode

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _unbound(tmp_path: Path) -> GrantSnapshot:
    result = compile_grant(
        PermissionFacts(
            revision=7,
            capabilities=(CapabilityFact("path.read", {"platform": "posix", "root": str(tmp_path)}),),
        ),
        task_id="task-binding",
        now=NOW,
    )
    assert result.allowed and result.grant is not None
    return result.grant


def _evidence(grant: GrantSnapshot, root: Path, *, identity: str | None = None, workspace_id: str | None = None):
    observed = identity or PathVerifier.identity_for(root)
    return VerifiedWorkspaceBinding(
        workspace_id=workspace_id or grant.workspace_identity.workspace_id,
        workspace_identity=observed,
        roots=tuple(
            VerifiedWorkspaceRoot(
                root_id=item.root_id,
                canonical_path=item.canonical_path,
                observed_identity=observed,
                reparse_identity=observed,
                reparse_safe=True,
            )
            for item in grant.workspace_roots
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", ("missing", "extra", "placeholder", "reparse", "workspace"))
def test_workspace_binder_rejects_incomplete_or_ambiguous_evidence(tmp_path: Path, case: str) -> None:
    grant = _unbound(tmp_path)
    evidence = _evidence(grant, tmp_path)
    if case == "missing":
        evidence = evidence.model_copy(update={"roots": ()})
    elif case == "extra":
        evidence = evidence.model_copy(
            update={
                "roots": (
                    *evidence.roots,
                    VerifiedWorkspaceRoot(
                        root_id="extra",
                        canonical_path=str(tmp_path / "extra"),
                        observed_identity="1:2",
                        reparse_identity="1:2",
                    ),
                )
            }
        )
    elif case == "placeholder":
        evidence = evidence.model_copy(
            update={"workspace_identity": "host-verification-required"}
        )
    elif case == "reparse":
        root = evidence.roots[0].model_copy(update={"reparse_identity": "different"})
        evidence = evidence.model_copy(update={"roots": (root,)})
    else:
        evidence = evidence.model_copy(update={"workspace_id": "other-workspace"})

    with pytest.raises(ValueError):
        bind_verified_workspace(grant, evidence)


@pytest.mark.unit
def test_workspace_binder_preserves_grant_and_real_file_allow_deny(tmp_path: Path) -> None:
    target = tmp_path / "allowed.txt"
    target.write_text("bound-safe", encoding="utf-8")
    grant = _unbound(tmp_path)
    bound = bind_verified_workspace(grant, _evidence(grant, tmp_path))

    assert bound.grant_id != grant.grant_id
    assert grant.grant_id in bound.grant_id
    assert bound.revision == grant.revision
    assert bound.policy_revision == grant.policy_revision
    assert bound.workspace_roots[0].rights == grant.workspace_roots[0].rights
    assert bound.workspace_roots[0].identity != "host-verification-required"

    context = HostContext(
        grant=bound,
        invocation=ToolInvocation(
            task_id=bound.task_id,
            operation_key=bound.operation_key,
            toolUseId="tool-bound-file",
            grantRevision=bound.revision,
            policyRevision=bound.policy_revision,
            workspace_identity=bound.workspace_identity,
        ),
        current_grant=lambda: bound,
        is_cancelled=lambda: False,
        now=NOW,
    )
    host = FileReadHost()
    root_id = bound.workspace_roots[0].root_id

    allowed = host.read_text(context, str(target), root_id=root_id)
    denied = host.read_text(context, str(tmp_path.parent / "outside.txt"), root_id=root_id)

    assert allowed.ok and allowed.value == "bound-safe"
    assert not denied.ok
    assert denied.decision.code is DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE


@pytest.mark.unit
def test_binding_only_accepts_compiler_unbound_snapshot(tmp_path: Path) -> None:
    grant = _unbound(tmp_path)
    bound = bind_verified_workspace(grant, _evidence(grant, tmp_path))
    with pytest.raises(ValueError, match="already host-bound"):
        bind_verified_workspace(bound, _evidence(bound, tmp_path))
