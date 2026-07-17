from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.agent_capabilities.catalog import freeze_grant
from app.agent_capabilities.command_network_hosts import (
    COMMAND_CANCELLED,
    COMMAND_TIMEOUT,
    NETWORK_TARGET_INVALID,
    UNSUPPORTED_P0_FAIL_CLOSED,
    CommandHost,
    HostInvocation,
    NetworkHost,
    NetworkResponse,
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
    PermissionRight,
    WorkspaceCapability,
    WorkspaceIdentity,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
IDENTITY = WorkspaceIdentity(workspace_id="ws-command-network", identity="root-identity")


def _grant(
    workspace: Path,
    *,
    executable: str = sys.executable,
    hosts: tuple[str, ...] = ("api.example", "cdn.example", "private.example"),
) -> object:
    return freeze_grant(
        GrantInput(
            grant_id="grant-command-network",
            revision=7,
            policy_revision=11,
            task_id="task-command-network",
            operation_key="op-command-network",
            workspace_identity=IDENTITY,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            workspace_roots=(
                WorkspaceCapability(
                    root_id="root-1",
                    canonical_path=str(workspace),
                    identity=IDENTITY.identity,
                    rights=(PermissionRight.READ, PermissionRight.WRITE, PermissionRight.DELETE),
                ),
            ),
            command=CommandCapability(
                mode="explicit",
                allowed_executables=(executable,),
                allowed_env_names=(),
                max_wall_seconds=2,
                max_output_bytes=4096,
            ),
            network=NetworkCapability(
                mode="allowlist",
                hosts=hosts,
                schemes=("https",),
                ports=(443,),
            ),
        )
    )


def _binding() -> InvocationBinding:
    return InvocationBinding(
        task_id="task-command-network",
        operation_key="op-command-network",
        workspace_identity=IDENTITY,
        policy_revision=11,
    )


def _command_invocation(grant: object, workspace: Path, argv: tuple[str, ...]) -> HostInvocation:
    request = CapabilityRequest(
        capability=CapabilityName.COMMAND_EXECUTE,
        binding=_binding(),
        command=CommandRequest(
            argv=argv,
            cwd=str(workspace),
            executable_identity_verified=True,
        ),
    )
    return HostInvocation(grant, request, "tool-command-1", 7)  # type: ignore[arg-type]


def _network_invocation(grant: object, host: str = "api.example") -> HostInvocation:
    request = CapabilityRequest(
        capability=CapabilityName.NETWORK_CONNECT,
        binding=_binding(),
        network=NetworkRequest(scheme="https", host=host, port=443),
    )
    return HostInvocation(grant, request, "tool-network-1", 7)  # type: ignore[arg-type]


@pytest.mark.unit
def test_command_uses_absolute_argv_no_shell_and_redacted_receipt(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    grant = _grant(tmp_path, executable=executable)
    invocation = _command_invocation(
        grant, tmp_path, (executable, "-c", "print('ok')", "token=super-secret")
    )

    result = CommandHost().execute(invocation, now=NOW)

    assert result.decision.code is DenyCode.ALLOWED
    assert result.stdout == b"ok\n"
    assert result.receipt.outcome == "allow"
    assert result.receipt.cleanup_verified is True
    rendered = repr(result.receipt.as_dict())
    assert "super-secret" not in rendered
    assert executable not in rendered
    assert result.receipt.redacted is True


@pytest.mark.unit
def test_command_rejects_path_fallback_and_p0_install_config_markers(tmp_path: Path) -> None:
    fallback_grant = _grant(tmp_path, executable="python3")
    fallback = CommandHost().execute(
        _command_invocation(fallback_grant, tmp_path, ("python3", "-c", "print('x')")), now=NOW
    )
    assert fallback.decision.code is DenyCode.TOOL_COMMAND_DENIED
    assert fallback.receipt.code == UNSUPPORTED_P0_FAIL_CLOSED
    assert fallback.stdout == b""

    executable = str(Path(sys.executable).resolve())
    p0_grant = _grant(tmp_path, executable=executable)
    p0 = CommandHost().execute(
        _command_invocation(p0_grant, tmp_path, (executable, "--global", "config")),
        now=NOW,
    )
    assert p0.decision.code is DenyCode.TOOL_COMMAND_DENIED
    assert p0.receipt.code == UNSUPPORTED_P0_FAIL_CLOSED
    assert p0.stdout == b""


@pytest.mark.unit
def test_command_cancel_kills_task_owned_process_group(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    grant = _grant(tmp_path, executable=executable)
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    invocation = _command_invocation(grant, tmp_path, (executable, "-c", parent_code))
    cancel = threading.Event()
    holder: list[object] = []

    def run() -> None:
        holder.append(CommandHost().execute(invocation, cancel_event=cancel, now=NOW))

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.2)
    cancel.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    result = holder[0]
    assert result.receipt.code == COMMAND_CANCELLED  # type: ignore[union-attr]
    assert result.receipt.cleanup_verified is True  # type: ignore[union-attr]


@pytest.mark.unit
def test_command_timeout_and_revoke_kill_task_owned_processes(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    grant = _grant(tmp_path, executable=executable)
    long_running = _command_invocation(
        grant, tmp_path, (executable, "-c", "import time; time.sleep(30)")
    )

    timeout_result = CommandHost().execute(long_running, now=NOW, active_policy_revision=11)
    assert timeout_result.receipt.code == COMMAND_TIMEOUT
    assert timeout_result.receipt.cleanup_verified is True

    revoked = threading.Event()
    holder: list[object] = []

    def run_revoked() -> None:
        holder.append(CommandHost().execute(long_running, revoked=revoked.is_set, now=NOW))

    thread = threading.Thread(target=run_revoked)
    thread.start()
    time.sleep(0.2)
    revoked.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    result = holder[0]
    assert result.receipt.code == "GRANT_REVOKED"  # type: ignore[union-attr]
    assert result.receipt.cleanup_verified is True  # type: ignore[union-attr]


@pytest.mark.unit
def test_network_revalidates_dns_and_policy_on_every_redirect() -> None:
    grant = _grant(Path("/tmp"))
    resolver_calls: list[tuple[str, int]] = []
    transport_calls: list[tuple[str, str]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        resolver_calls.append((host, port))
        return {"api.example": ("93.184.216.34",), "cdn.example": ("93.184.216.35",)}[host]

    def transport(*args: object) -> NetworkResponse:
        target = args[1]
        ip_address = args[2]
        transport_calls.append((target[2], ip_address))  # type: ignore[index]
        if len(transport_calls) == 1:
            return NetworkResponse(302, (("Location", "https://cdn.example/final"),))
        return NetworkResponse(200, (("Content-Type", "text/plain"),), b"network-ok")

    result = NetworkHost(resolver=resolver, transport=transport).request(
        _network_invocation(grant),
        path="/start",
        headers={"Authorization": "Bearer super-secret"},
        now=NOW,
    )

    assert result.decision.code is DenyCode.ALLOWED
    assert result.stdout == b"network-ok"
    assert resolver_calls == [("api.example", 443), ("cdn.example", 443)]
    assert transport_calls == [("api.example", "93.184.216.34"), ("cdn.example", "93.184.216.35")]
    assert result.receipt.redirects_verified == 1
    rendered = repr(result.receipt.as_dict())
    assert "super-secret" not in rendered
    assert "api.example" not in rendered


@pytest.mark.unit
def test_network_private_target_fails_closed_before_transport() -> None:
    grant = _grant(Path("/tmp"))
    calls: list[object] = []

    def transport(*args: object) -> NetworkResponse:
        calls.append(args)
        return NetworkResponse(200, (), b"must-not-run")

    result = NetworkHost(
        resolver=lambda host, port: ("127.0.0.1",),
        transport=transport,
    ).request(_network_invocation(grant, host="private.example"), now=NOW)

    assert result.decision.code is DenyCode.TOOL_NETWORK_DENIED
    assert result.receipt.code == NETWORK_TARGET_INVALID
    assert calls == []


@pytest.mark.unit
def test_network_grant_revision_mismatch_is_denied_without_transport() -> None:
    grant = _grant(Path("/tmp"))
    calls: list[object] = []
    invocation = _network_invocation(grant)
    mismatched = HostInvocation(invocation.grant, invocation.request, invocation.tool_use_id, 8)

    def transport(*args: object) -> NetworkResponse:
        calls.append(args)
        return NetworkResponse(200, (), b"must-not-run")

    result = NetworkHost(
        resolver=lambda host, port: ("93.184.216.34",),
        transport=transport,
    ).request(mismatched, now=NOW)

    assert result.decision.code is DenyCode.GRANT_REVISION_MISMATCH
    assert calls == []
