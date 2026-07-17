"""Fail-closed command and network capability hosts.

The B06P command/network host boundary is intentionally separate from the
B03 catalog.  B03 remains the only authority source: this module only adds
host verification immediately before a side effect and records a value-free
operation receipt afterwards.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import signal
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from .catalog import evaluate_capability
from .types import (
    CapabilityDecision,
    CapabilityName,
    CapabilityRequest,
    DecisionOutcome,
    DenyCode,
    GrantSnapshot,
    NetworkRequest,
)

UNSUPPORTED_P0_FAIL_CLOSED: Final[str] = "UNSUPPORTED_P0_FAIL_CLOSED"
COMMAND_CANCELLED: Final[str] = "COMMAND_CANCELLED"
COMMAND_TIMEOUT: Final[str] = "COMMAND_TIMEOUT"
REDIRECT_LIMIT_EXCEEDED: Final[str] = "REDIRECT_LIMIT_EXCEEDED"
NETWORK_TARGET_INVALID: Final[str] = "NETWORK_TARGET_INVALID"
PROCESS_CLEANUP_FAILED: Final[str] = "PROCESS_CLEANUP_FAILED"

_P0_ENV_NAMES = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH"})
_P0_WORDS = frozenset({"hook", "hooks", "home", "global", "install"})
_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class HostInvocation:
    """B-owned envelope for fields absent from the frozen B03 request model."""

    grant: GrantSnapshot
    request: CapabilityRequest
    tool_use_id: str
    grant_revision: int


@dataclass(frozen=True)
class OperationReceipt:
    """A value-free receipt; raw argv, URL, headers, and output never enter it."""

    receipt_id: str
    occurred_at: datetime
    capability: str
    task_id: str
    operation_key: str
    tool_use_id: str
    grant_id: str | None
    grant_revision: int | None
    policy_revision: int | None
    outcome: str
    code: str
    phase: str
    argv_digest: str | None = None
    target_digest: str | None = None
    output_bytes: int = 0
    error_bytes: int = 0
    redirects_verified: int = 0
    cleanup_verified: bool | None = None
    redacted: bool = True

    def as_dict(self) -> dict[str, object]:
        """Return only stable identifiers, digests, counters, and policy codes."""

        return {
            "receiptId": self.receipt_id,
            "occurredAt": self.occurred_at.isoformat(),
            "capability": self.capability,
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "toolUseId": self.tool_use_id,
            "grantId": self.grant_id,
            "grantRevision": self.grant_revision,
            "policyRevision": self.policy_revision,
            "outcome": self.outcome,
            "code": self.code,
            "phase": self.phase,
            "argvDigest": self.argv_digest,
            "targetDigest": self.target_digest,
            "outputBytes": self.output_bytes,
            "errorBytes": self.error_bytes,
            "redirectsVerified": self.redirects_verified,
            "cleanupVerified": self.cleanup_verified,
            "redacted": self.redacted,
        }


@dataclass(frozen=True)
class HostResult:
    decision: CapabilityDecision
    receipt: OperationReceipt
    stdout: bytes = b""
    stderr: bytes = b""
    response_headers: tuple[tuple[str, str], ...] = ()


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8", "backslashreplace")).hexdigest()


def _now(value: datetime | None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _invocation_fields(invocation: HostInvocation) -> tuple[str, str, str, str, str | None, int | None, int | None]:
    request = invocation.request
    grant = invocation.grant
    binding = request.binding
    capability = request.capability.value if isinstance(request.capability, CapabilityName) else str(request.capability)
    return (
        capability,
        binding.task_id,
        binding.operation_key,
        invocation.tool_use_id if isinstance(invocation.tool_use_id, str) else "",
        grant.grant_id if isinstance(grant, GrantSnapshot) else None,
        grant.revision if isinstance(grant, GrantSnapshot) else None,
        grant.policy_revision if isinstance(grant, GrantSnapshot) else None,
    )


def _receipt(
    invocation: HostInvocation,
    *,
    decision: CapabilityDecision,
    phase: str,
    code: str | None = None,
    occurred_at: datetime | None = None,
    argv_digest: str | None = None,
    target_digest: str | None = None,
    output_bytes: int = 0,
    error_bytes: int = 0,
    redirects_verified: int = 0,
    cleanup_verified: bool | None = None,
) -> OperationReceipt:
    capability, task_id, operation_key, tool_use_id, grant_id, grant_revision, policy_revision = _invocation_fields(invocation)
    return OperationReceipt(
        receipt_id=f"receipt_{uuid.uuid4().hex}",
        occurred_at=_now(occurred_at),
        capability=capability,
        task_id=task_id,
        operation_key=operation_key,
        tool_use_id=tool_use_id,
        grant_id=grant_id,
        grant_revision=grant_revision,
        policy_revision=policy_revision,
        outcome="allow" if decision.allowed and code is None else "deny",
        code=code or decision.code.value,
        phase=phase,
        argv_digest=argv_digest,
        target_digest=target_digest,
        output_bytes=output_bytes,
        error_bytes=error_bytes,
        redirects_verified=redirects_verified,
        cleanup_verified=cleanup_verified,
    )


def _denied_decision(invocation: HostInvocation, code: DenyCode) -> CapabilityDecision:
    request = invocation.request
    capability = request.capability.value if isinstance(request.capability, CapabilityName) else str(request.capability)
    return CapabilityDecision(
        outcome=DecisionOutcome.DENY,
        code=code,
        capability=capability,
        task_id=request.binding.task_id,
        operation_key=request.binding.operation_key,
        workspace_identity=request.binding.workspace_identity,
        grant_id=invocation.grant.grant_id,
        grant_revision=invocation.grant.revision,
        policy_revision=invocation.grant.policy_revision,
    )


def _preflight(
    invocation: HostInvocation,
    *,
    now: datetime | None,
    active_policy_revision: int | None,
) -> CapabilityDecision:
    """Check the B-owned envelope before calling the B03 pure policy."""

    grant = invocation.grant
    request = invocation.request
    if not isinstance(invocation.tool_use_id, str) or not invocation.tool_use_id or any(
        char in invocation.tool_use_id for char in "\x00\r\n"
    ):
        return _denied_decision(invocation, DenyCode.TOOL_CAPABILITY_DENIED)
    if invocation.grant_revision != grant.revision:
        return _denied_decision(invocation, DenyCode.GRANT_REVISION_MISMATCH)
    if (
        grant.task_id != request.binding.task_id
        or grant.operation_key != request.binding.operation_key
        or grant.workspace_identity != request.binding.workspace_identity
        or grant.policy_revision != request.binding.policy_revision
    ):
        return _denied_decision(invocation, DenyCode.GRANT_BINDING_MISMATCH)
    return evaluate_capability(
        grant,
        request,
        now=now,
        active_policy_revision=active_policy_revision,
    )


def _p0_reason(argv: Sequence[str], env_names: Sequence[str]) -> str | None:  # noqa: PLR0911
    """Reject known P0 discovery/install/config paths before process launch."""

    lowered = tuple(token.casefold() for token in argv)
    if any(name.upper() in _P0_ENV_NAMES for name in env_names):
        return "HOME_OR_PATH_DISCOVERY"
    if any(token in {"~", "$home", "%userprofile%"} or token.startswith(("~/", "$home/", "%userprofile%")) for token in lowered):
        return "HOME_DISCOVERY"
    if any(token in {"--global", "-g"} for token in lowered):
        return "GLOBAL_CONFIG_OR_INSTALL"
    if any("hook" in token for token in lowered):
        return "CLAUDE_HOOKS"
    if len(lowered) >= 2 and lowered[0].endswith("npm") and lowered[1] == "install":
        return "RUNTIME_NPM_INSTALL"
    if any(token in _P0_WORDS and token != "install" for token in lowered):
        return "GLOBAL_OR_HOME_DISCOVERY"
    return None


def _verify_cwd(cwd: str) -> bool:
    if not os.path.isabs(cwd) or any(char in cwd for char in "\x00\r\n"):
        return False
    path = Path(cwd)
    try:
        return path.exists() and path.is_dir() and not path.is_symlink() and path.resolve(strict=True) == path.absolute()
    except OSError:
        return False


def _verify_executable(path_text: str) -> bool:
    """Verify an explicit executable path without PATH lookup or expansion."""

    if not os.path.isabs(path_text) or any(char in path_text for char in "\x00\r\n~$%"):
        return False
    path = Path(path_text)
    try:
        if path.is_symlink() or not path.is_file():
            return False
        mode = path.stat().st_mode
        return bool(mode & (0o100 | 0o010 | 0o001))
    except OSError:
        return False


def _process_group_exists(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(process: subprocess.Popen[bytes], *, grace_seconds: float = 0.5) -> bool:  # noqa: PLR0911
    """Terminate a task-owned process group and prove its group is gone."""

    pid = process.pid
    if process.poll() is not None:
        return True
    if os.name == "posix":
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return True
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                return False
        return not _process_group_exists(pgid)

    # Windows has no portable process-group API in the stdlib.  taskkill is
    # invoked by argv only, with an absolute system path and no shell.
    taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], check=False, shell=False, capture_output=True, timeout=grace_seconds)
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


class CommandHost:
    """Execute only explicitly-authorized argv with task-owned cleanup."""

    def __init__(self, *, executable_verifier: Callable[[str], bool] | None = None) -> None:
        self._executable_verifier = executable_verifier or _verify_executable

    def execute(  # noqa: PLR0911
        self,
        invocation: HostInvocation,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: object | None = None,
        revoked: Callable[[], bool] | None = None,
        now: datetime | None = None,
        active_policy_revision: int | None = None,
    ) -> HostResult:
        decision = _preflight(invocation, now=now, active_policy_revision=active_policy_revision)
        command = invocation.request.command
        if not decision.allowed or command is None:
            return HostResult(decision, _receipt(invocation, decision=decision, phase="denied"))
        reason = _p0_reason(command.argv, command.env_names)
        if not os.path.isabs(command.argv[0]):
            reason = "PATH_FALLBACK"
        argv_digest = _digest(command.argv)
        if reason is not None or not command.executable_identity_verified:
            denied = _denied_decision(invocation, DenyCode.TOOL_COMMAND_DENIED)
            return HostResult(
                denied,
                _receipt(invocation, decision=denied, phase="preflight_denied", code=UNSUPPORTED_P0_FAIL_CLOSED if reason else None, argv_digest=argv_digest),
            )
        if not self._executable_verifier(command.argv[0]) or not _verify_cwd(command.cwd):
            denied = _denied_decision(invocation, DenyCode.HOST_VERIFICATION_REQUIRED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="host_verification_denied", argv_digest=argv_digest))
        supplied_environment = dict(environment or {})
        if set(supplied_environment) - set(command.env_names) or any(
            not isinstance(name, str) or not isinstance(value, str) or "\x00" in value
            for name, value in supplied_environment.items()
        ):
            denied = _denied_decision(invocation, DenyCode.TOOL_COMMAND_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="preflight_denied", argv_digest=argv_digest))
        if any(name not in supplied_environment for name in command.env_names):
            denied = _denied_decision(invocation, DenyCode.TOOL_COMMAND_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="preflight_denied", argv_digest=argv_digest))

        if _is_set(cancel_event) or (revoked is not None and revoked()):
            cancelled = _denied_decision(invocation, DenyCode.GRANT_REVOKED if revoked and revoked() else DenyCode.TOOL_CAPABILITY_DENIED)
            code = "GRANT_REVOKED" if revoked is not None and revoked() else COMMAND_CANCELLED
            return HostResult(cancelled, _receipt(invocation, decision=cancelled, phase="cancelled", code=code, argv_digest=argv_digest, cleanup_verified=True))

        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(command.argv),
                cwd=command.cwd,
                env=supplied_environment,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + min(float(invocation.grant.command.max_wall_seconds), 7200.0)
            while True:
                if _is_set(cancel_event) or (revoked is not None and revoked()):
                    cleanup = terminate_process_tree(process)
                    code = "GRANT_REVOKED" if revoked is not None and revoked() else COMMAND_CANCELLED
                    cancelled = _denied_decision(invocation, DenyCode.GRANT_REVOKED if code == "GRANT_REVOKED" else DenyCode.TOOL_CAPABILITY_DENIED)
                    return HostResult(cancelled, _receipt(invocation, decision=cancelled, phase="cancelled", code=code if cleanup else PROCESS_CLEANUP_FAILED, argv_digest=argv_digest, cleanup_verified=cleanup))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cleanup = terminate_process_tree(process)
                    timed_out = _denied_decision(invocation, DenyCode.TOOL_CAPABILITY_DENIED)
                    return HostResult(timed_out, _receipt(invocation, decision=timed_out, phase="timed_out", code=COMMAND_TIMEOUT if cleanup else PROCESS_CLEANUP_FAILED, argv_digest=argv_digest, cleanup_verified=cleanup))
                try:
                    stdout, stderr = process.communicate(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
            max_bytes = invocation.grant.command.max_output_bytes
            stdout = stdout[:max_bytes]
            stderr = stderr[:max_bytes]
            completed = _receipt(invocation, decision=decision, phase="completed", argv_digest=argv_digest, output_bytes=len(stdout), error_bytes=len(stderr), cleanup_verified=True)
            return HostResult(decision, completed, stdout, stderr)
        except (OSError, ValueError):
            cleanup = terminate_process_tree(process) if process is not None else True
            failed = _denied_decision(invocation, DenyCode.TOOL_COMMAND_DENIED)
            return HostResult(failed, _receipt(invocation, decision=failed, phase="failed", code=PROCESS_CLEANUP_FAILED if not cleanup else "COMMAND_EXECUTION_FAILED", argv_digest=argv_digest, cleanup_verified=cleanup))


class NetworkTransport(Protocol):
    def __call__(
        self,
        method: str,
        target: tuple[str, int, str, str],
        ip_address: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_bytes: int,
    ) -> NetworkResponse: ...


@dataclass(frozen=True)
class NetworkResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str | None:
        wanted = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == wanted), None)


def _is_public_address(value: str, *, allow_private: bool) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if allow_private:
        return not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(item[4][0]) for item in addresses))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip_address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip_address = ip_address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip_address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip_address = ip_address
        self._ssl_context = ssl.create_default_context()
        self._server_hostname = host

    def connect(self) -> None:
        raw = socket.create_connection((self._ip_address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self._server_hostname)


def _default_transport(
    method: str,
    target: tuple[str, int, str, str],
    ip_address: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_bytes: int,
) -> NetworkResponse:
    scheme, port, host, path = target
    connection: http.client.HTTPConnection
    if scheme == "https":
        connection = _PinnedHTTPSConnection(host, port, ip_address, timeout_seconds)
    else:
        connection = _PinnedHTTPConnection(host, port, ip_address, timeout_seconds)
    safe_headers = {key: value for key, value in headers.items() if key.casefold() != "host"}
    safe_headers["Host"] = host
    try:
        connection.request(method, path, body=body, headers=safe_headers)
        response = connection.getresponse()
        payload = response.read(max_bytes + 1)
        return NetworkResponse(response.status, tuple(response.getheaders()), payload)
    finally:
        connection.close()


def _target_from_url(url: str) -> tuple[str, str, int, str] | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    if "\r" in path or "\n" in path:
        return None
    return parts.scheme, parts.hostname, port, path


class NetworkHost:
    """Perform pinned-IP requests with per-hop grant and DNS verification."""

    def __init__(
        self,
        *,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        transport: NetworkTransport | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver
        self._transport = transport or _default_transport

    def request(  # noqa: PLR0911, PLR0912
        self,
        invocation: HostInvocation,
        *,
        method: str = "GET",
        path: str = "/",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        revoked: Callable[[], bool] | None = None,
        now: datetime | None = None,
        active_policy_revision: int | None = None,
    ) -> HostResult:
        decision = _preflight(invocation, now=now, active_policy_revision=active_policy_revision)
        network = invocation.request.network
        if network is None or (not decision.allowed and decision.code is not DenyCode.HOST_VERIFICATION_REQUIRED):
            return HostResult(decision, _receipt(invocation, decision=decision, phase="denied"))
        if invocation.grant.network.mode == "deny":
            return HostResult(decision, _receipt(invocation, decision=decision, phase="denied"))
        if not isinstance(method, str) or not method or any(char in method for char in "\x00\r\n"):
            denied = _denied_decision(invocation, DenyCode.TOOL_NETWORK_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="preflight_denied"))
        if not path.startswith("/") or any(char in path for char in "\x00\r\n"):
            denied = _denied_decision(invocation, DenyCode.TOOL_NETWORK_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="preflight_denied"))
        if _p0_reason((network.host, path), ()) is not None:
            denied = _denied_decision(invocation, DenyCode.TOOL_NETWORK_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="preflight_denied", code=UNSUPPORTED_P0_FAIL_CLOSED))
        target_url = f"{network.scheme}://{network.host}:{network.port}{path}"
        target = _target_from_url(target_url)
        target_digest = _digest(target_url)
        if target is None:
            denied = _denied_decision(invocation, DenyCode.TOOL_NETWORK_DENIED)
            return HostResult(denied, _receipt(invocation, decision=denied, phase="host_verification_denied", target_digest=target_digest))

        current_scheme, current_host, current_port, current_path = target
        redirects_verified = 0
        max_bytes = invocation.grant.budget.max_tool_output_bytes
        while True:
            if revoked is not None and revoked():
                cancelled = _denied_decision(invocation, DenyCode.GRANT_REVOKED)
                return HostResult(cancelled, _receipt(invocation, decision=cancelled, phase="cancelled", code="GRANT_REVOKED", target_digest=target_digest, redirects_verified=redirects_verified))
            try:
                resolved = tuple(dict.fromkeys(str(value) for value in self._resolver(current_host, current_port)))
            except (OSError, socket.gaierror, ValueError):
                resolved = ()
            if not resolved or not all(_is_public_address(value, allow_private=invocation.grant.network.allow_private_addresses) for value in resolved):
                denied = _denied_decision(invocation, DenyCode.TOOL_NETWORK_DENIED)
                return HostResult(denied, _receipt(invocation, decision=denied, phase="host_verification_denied", code=NETWORK_TARGET_INVALID, target_digest=target_digest, redirects_verified=redirects_verified))
            current_request = invocation.request.model_copy(
                update={
                    "network": NetworkRequest(
                        scheme=current_scheme,
                        host=current_host,
                        port=current_port,
                        resolved_addresses=resolved,
                    )
                }
            )
            current_invocation = HostInvocation(invocation.grant, current_request, invocation.tool_use_id, invocation.grant_revision)
            current_decision = _preflight(current_invocation, now=now, active_policy_revision=active_policy_revision)
            if not current_decision.allowed:
                return HostResult(current_decision, _receipt(current_invocation, decision=current_decision, phase="denied", target_digest=target_digest, redirects_verified=redirects_verified))
            try:
                response = self._transport(
                    method,
                    (current_scheme, current_port, current_host, current_path),
                    resolved[0],
                    headers or {},
                    body,
                    min(float(invocation.grant.budget.wall_seconds), 7200.0),
                    max_bytes,
                )
            except (OSError, ValueError, http.client.HTTPException):
                failed = _denied_decision(current_invocation, DenyCode.TOOL_NETWORK_DENIED)
                return HostResult(failed, _receipt(current_invocation, decision=failed, phase="failed", code="NETWORK_REQUEST_FAILED", target_digest=target_digest, redirects_verified=redirects_verified))
            if response.status not in {301, 302, 303, 307, 308}:
                payload = response.body[:max_bytes]
                return HostResult(
                    current_decision,
                    _receipt(current_invocation, decision=current_decision, phase="completed", target_digest=target_digest, output_bytes=len(payload), redirects_verified=redirects_verified),
                    stdout=payload,
                    response_headers=response.headers,
                )
            location = response.header("location")
            if location is None or redirects_verified >= _MAX_REDIRECTS:
                failed = _denied_decision(current_invocation, DenyCode.TOOL_NETWORK_DENIED)
                return HostResult(failed, _receipt(current_invocation, decision=failed, phase="redirect_denied", code=REDIRECT_LIMIT_EXCEEDED if location else NETWORK_TARGET_INVALID, target_digest=target_digest, redirects_verified=redirects_verified))
            next_url = urljoin(f"{current_scheme}://{current_host}:{current_port}{current_path}", location)
            next_target = _target_from_url(next_url)
            if next_target is None:
                failed = _denied_decision(current_invocation, DenyCode.TOOL_NETWORK_DENIED)
                return HostResult(failed, _receipt(current_invocation, decision=failed, phase="redirect_denied", code=NETWORK_TARGET_INVALID, target_digest=target_digest, redirects_verified=redirects_verified))
            current_scheme, current_host, current_port, current_path = next_target
            redirects_verified += 1


def _is_set(value: object | None) -> bool:
    return bool(value is not None and getattr(value, "is_set", lambda: False)())


__all__ = [
    "COMMAND_CANCELLED",
    "COMMAND_TIMEOUT",
    "PROCESS_CLEANUP_FAILED",
    "REDIRECT_LIMIT_EXCEEDED",
    "UNSUPPORTED_P0_FAIL_CLOSED",
    "CommandHost",
    "HostInvocation",
    "HostResult",
    "NetworkHost",
    "NetworkResponse",
    "OperationReceipt",
    "terminate_process_tree",
]
