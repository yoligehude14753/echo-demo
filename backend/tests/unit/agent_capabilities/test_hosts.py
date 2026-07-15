"""B06P-A focused contract and real filesystem harness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agent_capabilities.hosts import (
    AtomicMutationHost,
    FileReadHost,
    HostContext,
    PathVerifier,
    ToolInvocation,
)
from app.agent_capabilities.types import (
    DenyCode,
    GrantInput,
    GrantSnapshot,
    PermissionRight,
    WorkspaceCapability,
    WorkspaceIdentity,
)

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


class Control:
    def __init__(self, grant: GrantSnapshot) -> None:
        self.current: GrantSnapshot | None = grant
        self.cancelled = False

    def snapshot(self) -> GrantSnapshot | None:
        return self.current


def _grant(root: Path, *, revision: int = 7) -> tuple[GrantSnapshot, str]:
    root_id = "root-test"
    identity = PathVerifier.identity_for(root)
    grant = GrantSnapshot.from_input(
        GrantInput(
            grant_id=f"grant-test-{revision}",
            revision=revision,
            policy_revision=revision,
            task_id="task-test",
            operation_key="op-test",
            workspace_identity=WorkspaceIdentity(workspace_id="workspace-test", identity="workspace-1"),
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            workspace_roots=(
                WorkspaceCapability(
                    root_id=root_id,
                    canonical_path=str(root),
                    identity=identity,
                    rights=(PermissionRight.READ, PermissionRight.WRITE, PermissionRight.DELETE),
                ),
            ),
        )
    )
    return grant, root_id


def _context(grant: GrantSnapshot, control: Control, *, grant_revision: int | None = None) -> HostContext:
    return HostContext(
        grant=grant,
        invocation=ToolInvocation(
            task_id=grant.task_id,
            operation_key=grant.operation_key,
            toolUseId="tool-test-1",
            grantRevision=grant_revision or grant.revision,
            policyRevision=grant.policy_revision,
            workspace_identity=grant.workspace_identity,
        ),
        current_grant=control.snapshot,
        is_cancelled=lambda: control.cancelled,
        now=NOW,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "provider", "cancelled", "grant_revision", "expected"),
    [
        ("missing-current-snapshot", None, False, 7, DenyCode.GRANT_REVOKED),
        ("cancelled", "same", True, 7, DenyCode.GRANT_REVOKED),
        ("revision-mismatch", "same", False, 8, DenyCode.GRANT_REVISION_MISMATCH),
        ("revoked", "revoked", False, 7, DenyCode.GRANT_REVOKED),
    ],
)
def test_contract_rechecks_snapshot_binding_and_cancel_before_read(
    tmp_path: Path,
    case: str,
    provider: str | None,
    cancelled: bool,
    grant_revision: int,
    expected: DenyCode,
) -> None:
    del case
    path = tmp_path / "note.txt"
    path.write_text("safe", encoding="utf-8")
    grant, root_id = _grant(tmp_path)
    control = Control(grant)
    if provider is None or provider == "revoked":
        control.current = None
    control.cancelled = cancelled
    context = _context(grant, control, grant_revision=grant_revision)

    result = FileReadHost().read_bytes(context, str(path), root_id=root_id)

    assert not result.ok
    assert result.decision.code is expected
    assert result.receipt.tool_use_id == "tool-test-1"
    assert result.receipt.grant_revision == 7


@pytest.mark.unit
def test_contract_rejects_outside_and_symlink_paths_and_receipt_is_value_free(tmp_path: Path) -> None:
    inside = tmp_path / "inside.txt"
    inside.write_text("super-secret-content", encoding="utf-8")
    outside = tmp_path.parent / "outside-b06p.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    grant, root_id = _grant(tmp_path)
    control = Control(grant)
    host = FileReadHost()

    outside_result = host.read_bytes(_context(grant, control), str(outside), root_id=root_id)
    link_result = host.read_bytes(_context(grant, control), str(link), root_id=root_id)
    good_result = host.read_bytes(_context(grant, control), str(inside), root_id=root_id)

    assert outside_result.decision.code is DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE
    assert link_result.decision.code is DenyCode.TOOL_PATH_AMBIGUOUS
    assert good_result.ok and good_result.value == b"super-secret-content"
    rendered = good_result.receipt.model_dump_json(by_alias=True)
    assert "super-secret-content" not in rendered
    assert str(inside) not in rendered
    assert "toolUseId" in rendered and "grantRevision" in rendered


@pytest.mark.unit
def test_contract_glob_and_grep_are_bound_to_one_read_receipt(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("keep\nneedle here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle again\n", encoding="utf-8")
    grant, root_id = _grant(tmp_path)
    control = Control(grant)
    host = FileReadHost()
    context = _context(grant, control)

    listed = host.glob(context, "*.txt", root_id=root_id)
    matches = host.grep(context, "needle", listed.value or (), root_id=root_id)

    assert listed.ok and listed.value is not None and len(listed.value) == 2
    assert matches.ok and matches.value is not None and len(matches.value) == 2
    assert matches.receipt.operation == "file.grep"
    assert any("pattern_digest" in key for key, _ in matches.receipt.metadata)


@pytest.mark.unit
def test_real_mutation_harness_is_atomic_and_cleans_cancelled_temp(tmp_path: Path) -> None:
    grant, root_id = _grant(tmp_path)
    control = Control(grant)
    context = _context(grant, control)
    host = AtomicMutationHost()
    target = tmp_path / "atomic.txt"

    written = host.write_text(context, str(target), "one", root_id=root_id)
    assert written.ok and target.read_text(encoding="utf-8") == "one"
    assert not list(tmp_path.glob(".echodesk-b06p-*"))

    patched = host.patch_text(_context(grant, control), str(target), "one", "two", root_id=root_id)
    assert patched.ok and target.read_text(encoding="utf-8") == "two"

    control.current = None
    revoked = AtomicMutationHost(before_commit=lambda: None).write_text(
        _context(grant, control), str(tmp_path / "revoked.txt"), "blocked", root_id=root_id
    )
    assert not revoked.ok and revoked.decision.code is DenyCode.GRANT_REVOKED
    assert not (tmp_path / "revoked.txt").exists()
    assert not list(tmp_path.glob(".echodesk-b06p-*"))


@pytest.mark.unit
def test_real_mutation_harness_rechecks_revoke_after_staging(tmp_path: Path) -> None:
    grant, root_id = _grant(tmp_path)
    control = Control(grant)

    def revoke() -> None:
        control.current = None

    target = tmp_path / "rechecked.txt"
    result = AtomicMutationHost(before_commit=revoke).write_text(
        _context(grant, control), str(target), "must-not-commit", root_id=root_id
    )

    assert not result.ok
    assert result.decision.code is DenyCode.GRANT_REVOKED
    assert not target.exists()
    assert not list(tmp_path.glob(".echodesk-b06p-*"))


@pytest.mark.unit
def test_real_mutation_harness_rejects_symlink_and_deletes_only_verified_file(tmp_path: Path) -> None:
    grant, root_id = _grant(tmp_path)
    control = Control(grant)
    outside = tmp_path.parent / "delete-outside-b06p.txt"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "link-delete.txt"
    link.symlink_to(outside)
    host = AtomicMutationHost()

    denied_link = host.delete(_context(grant, control), str(link), root_id=root_id)
    assert not denied_link.ok and denied_link.decision.code is DenyCode.TOOL_PATH_AMBIGUOUS
    assert outside.exists()

    target = tmp_path / "delete.txt"
    target.write_text("remove", encoding="utf-8")
    deleted = host.delete(_context(grant, control), str(target), root_id=root_id)
    assert deleted.ok and not target.exists()
