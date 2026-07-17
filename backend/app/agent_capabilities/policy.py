"""Pure policy primitives for the Echo capability grant compiler.

This module deliberately contains no host inspection.  Paths are normalized
lexically, command authority is argv-based (never shell text), and network
hostname decisions require caller-supplied verification evidence.
"""

from __future__ import annotations

import ipaddress
import ntpath
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from .types import CapabilityName


class DecisionStatus(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HOST_VERIFICATION_REQUIRED = "HOST_VERIFICATION_REQUIRED"


class ReasonCode(StrEnum):
    ALLOWED = "ALLOWED"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    INVALID_INPUT = "INVALID_INPUT"
    AMBIGUOUS_INPUT = "AMBIGUOUS_INPUT"
    CONFLICTING_SCOPE = "CONFLICTING_SCOPE"
    STALE_REVISION = "STALE_REVISION"
    EXPIRED_REVISION = "EXPIRED_REVISION"
    REVISION_FRESHNESS_UNVERIFIED = "REVISION_FRESHNESS_UNVERIFIED"
    PATH_OUTSIDE_ROOT = "PATH_OUTSIDE_ROOT"
    COMMAND_NOT_AUTHORIZED = "COMMAND_NOT_AUTHORIZED"
    NETWORK_NOT_AUTHORIZED = "NETWORK_NOT_AUTHORIZED"
    NETWORK_SCHEME_NOT_ALLOWED = "NETWORK_SCHEME_NOT_ALLOWED"
    NETWORK_SSRF_BLOCKED = "NETWORK_SSRF_BLOCKED"
    REDIRECT_NOT_AUTHORIZED = "REDIRECT_NOT_AUTHORIZED"
    SKILL_NOT_AUTHORIZED = "SKILL_NOT_AUTHORIZED"
    SKILL_PROVENANCE_REQUIRED = "SKILL_PROVENANCE_REQUIRED"


@dataclass(frozen=True)
class Decision:
    """The only result shape used by policy decisions."""

    status: DecisionStatus
    reason_code: ReasonCode
    capability: str | None = None
    detail: str = ""
    normalized: object | None = None

    @property
    def allowed(self) -> bool:
        return self.status is DecisionStatus.ALLOW

    @property
    def host_verification_required(self) -> bool:
        return self.status is DecisionStatus.HOST_VERIFICATION_REQUIRED


class PolicyInputError(ValueError):
    """Raised by normalizers; the decision API converts it to DENY."""

    def __init__(self, reason_code: ReasonCode, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class PathRoot:
    platform: str
    root: str
    case_sensitive: bool


@dataclass(frozen=True)
class PathScope:
    roots: tuple[PathRoot, ...]


@dataclass(frozen=True)
class PathRequest:
    platform: str
    path: str


@dataclass(frozen=True)
class CommandScope:
    platform: str
    argv: tuple[str, ...]
    cwd: str
    env_names: tuple[str, ...]


CommandRequest = CommandScope


@dataclass(frozen=True)
class NetworkTarget:
    scheme: str
    host: str
    port: int
    verified_ips: tuple[str, ...] = ()
    host_verification_required: bool = False


@dataclass(frozen=True)
class NetworkScope:
    target: NetworkTarget
    allowed_redirects: tuple[NetworkTarget, ...] = ()


@dataclass(frozen=True)
class NetworkRequest:
    target: NetworkTarget
    redirects: tuple[NetworkTarget, ...] = ()


@dataclass(frozen=True)
class SkillScope:
    identity: str
    version: str
    provenance: str


SkillRequest = SkillScope


@dataclass(frozen=True)
class CapabilityFact:
    capability: str
    scope: object
    effect: str = "allow"


@dataclass(frozen=True)
class PermissionFacts:
    """Echo-owned permission facts supplied to the pure compiler."""

    revision: int
    capabilities: tuple[CapabilityFact, ...]
    issued_at: datetime | str | None = None
    expires_at: datetime | str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if self.issued_at is not None:
            object.__setattr__(self, "issued_at", _parse_time(self.issued_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _parse_time(self.expires_at))


EchoPermissionFacts = PermissionFacts


_CAPABILITY_ALIASES = {item.value: item.value for item in CapabilityName}
SUPPORTED_CAPABILITIES = frozenset(_CAPABILITY_ALIASES.values())

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHELL_AMBIGUOUS = frozenset("\n\r\x00")
_WILDCARD = frozenset("*?[]")


def canonical_capability(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyInputError(ReasonCode.UNKNOWN_CAPABILITY, "capability is blank")
    key = value.strip().lower()
    try:
        return _CAPABILITY_ALIASES[key]
    except KeyError as exc:
        raise PolicyInputError(
            ReasonCode.UNKNOWN_CAPABILITY, f"unknown capability: {value!r}"
        ) from exc


def normalize_path_root(root: str, *, platform: str) -> PathRoot:
    """Normalize an absolute root without touching the filesystem."""

    platform = _platform(platform)
    _validate_path_text(root)
    if any(char in root for char in "~$%*?[]"):
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "path contains expansion or glob syntax")
    if platform == "posix":
        if not root.startswith("/") or root.startswith("//") or "\\" in root:
            raise PolicyInputError(
                ReasonCode.AMBIGUOUS_INPUT, "path is not an unambiguous POSIX absolute path"
            )
        normalized = posixpath.normpath(root)
        if not normalized.startswith("/"):
            raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "POSIX root escaped its anchor")
        return PathRoot(platform="posix", root=normalized, case_sensitive=True)

    if root.startswith(("\\\\?\\", "\\\\.\\")):
        raise PolicyInputError(
            ReasonCode.AMBIGUOUS_INPUT, "Windows device paths are host dependent"
        )
    drive, tail = ntpath.splitdrive(root)
    if not drive or not tail.startswith(("\\", "/")):
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "path is not an absolute Windows root")
    normalized = ntpath.normpath(root).replace("/", "\\")
    if normalized.startswith("\\\\"):
        parts = [part for part in normalized[2:].split("\\") if part]
        if len(parts) < 2:
            raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "UNC root lacks a share")
    return PathRoot(platform="windows", root=normalized, case_sensitive=False)


def normalize_path_scope(scope: PathScope | Mapping[str, Any]) -> PathScope:
    roots_value: Any
    if isinstance(scope, PathScope):
        roots_value = scope.roots
    elif isinstance(scope, Mapping):
        if "roots" in scope:
            roots_value = scope["roots"]
        elif "root" in scope:
            roots_value = ({"root": scope["root"], "platform": scope.get("platform")},)
        else:
            roots_value = None
    else:
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "path scope must be a mapping or PathScope"
        )
    if isinstance(roots_value, (str, PathRoot)) or roots_value is None:
        roots_value = (roots_value,)
    roots: list[PathRoot] = []
    for value in roots_value:
        if isinstance(value, PathRoot):
            root = normalize_path_root(value.root, platform=value.platform)
        elif isinstance(value, Mapping):
            root = normalize_path_root(str(value["root"]), platform=str(value["platform"]))
        else:
            raise PolicyInputError(ReasonCode.INVALID_INPUT, "path root needs platform and root")
        roots.append(root)
    if not roots:
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "path scope has no roots")
    return PathScope(roots=tuple(sorted(set(roots), key=lambda item: (item.platform, item.root))))


def normalize_path_request(path: str, *, platform: str) -> PathRequest:
    root = normalize_path_root(path, platform=platform)
    return PathRequest(platform=root.platform, path=root.root)


def path_is_within_root(root: PathRoot, path: PathRequest | str) -> bool:
    request = (
        path
        if isinstance(path, PathRequest)
        else normalize_path_request(path, platform=root.platform)
    )
    if request.platform != root.platform:
        return False
    candidate = request.path.casefold() if not root.case_sensitive else request.path
    anchor = root.root.casefold() if not root.case_sensitive else root.root
    if candidate == anchor:
        return True
    prefix = (
        anchor
        if anchor.endswith(("/", "\\"))
        else anchor + ("\\" if root.platform == "windows" else "/")
    )
    return candidate.startswith(prefix)


def normalize_command_scope(
    argv: Iterable[str],
    *,
    cwd: str,
    env_names: Iterable[str] = (),
    platform: str,
) -> CommandScope:
    platform = _platform(platform)
    if isinstance(argv, str):
        raise PolicyInputError(
            ReasonCode.AMBIGUOUS_INPUT, "command must be an argv sequence, not shell text"
        )
    argv_tuple = tuple(argv)
    if not argv_tuple or any(
        not isinstance(arg, str) or not arg or any(char in arg for char in _SHELL_AMBIGUOUS)
        for arg in argv_tuple
    ):
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "argv must contain non-empty strings")
    executable = argv_tuple[0]
    if any(char in executable for char in _WILDCARD) or any(char.isspace() for char in executable):
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "executable name is ambiguous")
    normalized_cwd = normalize_path_request(cwd, platform=platform).path
    normalized_env = tuple(sorted(set(env_names)))
    if any(not isinstance(name, str) or not _ENV_NAME.fullmatch(name) for name in normalized_env):
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "env_names must be variable names, not assignments"
        )
    return CommandScope(
        platform=platform, argv=argv_tuple, cwd=normalized_cwd, env_names=normalized_env
    )


def command_is_authorized(authority: CommandScope, request: CommandScope) -> bool:
    return (
        authority.platform == request.platform
        and authority.argv == request.argv
        and authority.cwd == request.cwd
        and set(request.env_names).issubset(authority.env_names)
    )


def normalize_network_target(
    scheme: str,
    host: str,
    port: int | None = None,
    *,
    verified_ips: Iterable[str] = (),
) -> NetworkTarget:
    if not isinstance(scheme, str) or scheme.strip().lower() not in {"http", "https"}:
        raise PolicyInputError(
            ReasonCode.NETWORK_SCHEME_NOT_ALLOWED, "only http and https are supported"
        )
    scheme = scheme.strip().lower()
    host = _normalize_host(host)
    normalized_ips = _normalize_verified_ips(verified_ips)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise PolicyInputError(
                ReasonCode.NETWORK_SSRF_BLOCKED, "non-public literal address is blocked"
            )
        requires_verification = False
    else:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".local", ".internal", ".localhost")
        ):
            raise PolicyInputError(ReasonCode.NETWORK_SSRF_BLOCKED, "local hostname is blocked")
        requires_verification = not bool(normalized_ips)
    expected_port = 443 if scheme == "https" else 80
    if port is None:
        port = expected_port
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "network port must be in 1..65535")
    return NetworkTarget(
        scheme=scheme,
        host=host,
        port=port,
        verified_ips=normalized_ips,
        host_verification_required=requires_verification,
    )


def normalize_network_scope(scope: NetworkScope | Mapping[str, Any]) -> NetworkScope:
    if isinstance(scope, NetworkScope):
        target_value: Any = scope.target
        redirects_value: Any = scope.allowed_redirects
    elif isinstance(scope, Mapping):
        target_value = scope.get("target", scope)
        redirects_value = scope.get("allowed_redirects", ())
    else:
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "network scope must be a mapping or NetworkScope"
        )
    target = _normalize_network_value(target_value)
    redirects = tuple(_normalize_network_value(value) for value in redirects_value)
    return NetworkScope(target=target, allowed_redirects=redirects)


def normalize_network_request(request: NetworkRequest | Mapping[str, Any]) -> NetworkRequest:
    if isinstance(request, NetworkRequest):
        target = _normalize_network_value(request.target)
        redirects_value: Any = request.redirects
    elif isinstance(request, Mapping):
        target = _normalize_network_value(request.get("target", request))
        redirects_value = request.get("redirects", ())
    else:
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "network request must be a mapping or NetworkRequest"
        )
    return NetworkRequest(
        target=target, redirects=tuple(_normalize_network_value(value) for value in redirects_value)
    )


def network_is_authorized(authority: NetworkScope, request: NetworkRequest) -> Decision:
    if request.target.host_verification_required:
        return Decision(
            DecisionStatus.HOST_VERIFICATION_REQUIRED,
            ReasonCode.AMBIGUOUS_INPUT,
            "network.connect",
            "hostname resolution evidence is required",
            request,
        )
    if authority.target != request.target:
        return Decision(
            DecisionStatus.DENY,
            ReasonCode.NETWORK_NOT_AUTHORIZED,
            "network.connect",
            "scheme, host, port, or verification differs",
            request,
        )
    if request.redirects:
        if not authority.allowed_redirects:
            return Decision(
                DecisionStatus.DENY,
                ReasonCode.REDIRECT_NOT_AUTHORIZED,
                "network.connect",
                "redirect targets require explicit grant scope",
                request,
            )
        if any(redirect not in authority.allowed_redirects for redirect in request.redirects):
            return Decision(
                DecisionStatus.DENY,
                ReasonCode.REDIRECT_NOT_AUTHORIZED,
                "network.connect",
                "redirect target is outside the explicit grant scope",
                request,
            )
    return Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, "network.connect", normalized=request)


def normalize_skill_scope(scope: SkillScope | Mapping[str, Any]) -> SkillScope:
    if isinstance(scope, SkillScope):
        identity, version, provenance = scope.identity, scope.version, scope.provenance
    elif isinstance(scope, Mapping):
        identity = scope.get("identity")
        version = scope.get("version")
        provenance = scope.get("provenance", "")
    else:
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "skill scope must be a mapping or SkillScope"
        )
    if (
        not isinstance(identity, str)
        or not identity
        or any(char.isspace() or char in "\\\x00" for char in identity)
    ):
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "skill identity is not canonical")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise PolicyInputError(
            ReasonCode.AMBIGUOUS_INPUT, "skill version must be an exact semantic version"
        )
    if (
        not isinstance(provenance, str)
        or not provenance
        or any(char in provenance for char in "\r\n\x00")
    ):
        raise PolicyInputError(ReasonCode.SKILL_PROVENANCE_REQUIRED, "skill provenance is required")
    return SkillScope(identity=identity, version=version, provenance=provenance)


def skill_is_authorized(authority: SkillScope, request: SkillScope) -> Decision:
    if authority != request:
        return Decision(
            DecisionStatus.DENY,
            ReasonCode.SKILL_NOT_AUTHORIZED,
            "skill.use",
            "skill identity, version, or provenance differs",
            request,
        )
    return Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, "skill.use", normalized=request)


def _normalize_network_value(value: NetworkTarget | Mapping[str, Any]) -> NetworkTarget:
    if isinstance(value, NetworkTarget):
        return normalize_network_target(
            value.scheme, value.host, value.port, verified_ips=value.verified_ips
        )
    if not isinstance(value, Mapping):
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "network target must be a mapping or NetworkTarget"
        )
    return normalize_network_target(
        str(value.get("scheme", "")),
        str(value.get("host", "")),
        value.get("port"),
        verified_ips=value.get("verified_ips", ()),
    )


def _normalize_host(host: str) -> str:
    if (
        not isinstance(host, str)
        or not host
        or any(char.isspace() or char in "/?#@" for char in host)
    ):
        raise PolicyInputError(
            ReasonCode.AMBIGUOUS_INPUT, "host contains URL syntax outside scheme/host/port"
        )
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith("."):
        host = host[:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise PolicyInputError(
                ReasonCode.AMBIGUOUS_INPUT, "host cannot be IDNA-normalized"
            ) from exc


def _normalize_verified_ips(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "verified_ips must be a sequence")
    result: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise PolicyInputError(
                ReasonCode.AMBIGUOUS_INPUT, "verified_ips contains a non-IP value"
            ) from exc
        if not address.is_global:
            raise PolicyInputError(
                ReasonCode.NETWORK_SSRF_BLOCKED,
                "verified hostname resolved to a non-public address",
            )
        result.append(address.compressed)
    return tuple(sorted(set(result)))


def _platform(value: str) -> str:
    if value not in {"posix", "windows"}:
        raise PolicyInputError(
            ReasonCode.AMBIGUOUS_INPUT, "platform must be explicitly posix or windows"
        )
    return value


def _validate_path_text(value: str) -> None:
    if not isinstance(value, str) or not value or any(char in value for char in _SHELL_AMBIGUOUS):
        raise PolicyInputError(
            ReasonCode.INVALID_INPUT, "path must be a non-empty string without NUL/control breaks"
        )


def _parse_time(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PolicyInputError(ReasonCode.INVALID_INPUT, "expires_at is not ISO-8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "expires_at needs an explicit timezone")
    return value.astimezone(UTC)


T = TypeVar("T")


def allow(capability: str, normalized: T, *, detail: str = "") -> Decision:
    return Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, capability, detail, normalized)


def deny(capability: str | None, reason: ReasonCode, detail: str) -> Decision:
    return Decision(DecisionStatus.DENY, reason, capability, detail)


def host_verification_required(
    capability: str, detail: str, normalized: object | None = None
) -> Decision:
    return Decision(
        DecisionStatus.HOST_VERIFICATION_REQUIRED,
        ReasonCode.AMBIGUOUS_INPUT,
        capability,
        detail,
        normalized,
    )
