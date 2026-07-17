"""Frozen capability catalog and side-effect-free policy decisions."""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

from .types import (
    CATALOG_SCHEMA_VERSION,
    AgentResourceBudget,
    CapabilityDecision,
    CapabilityName,
    CapabilityRequest,
    DecisionOutcome,
    DenyCode,
    GrantInput,
    GrantSnapshot,
    PermissionRight,
    SkillRequest,
    VerifiedWorkspaceBinding,
    WorkspaceCapability,
)


class CapabilitySpec:
    """Frozen catalog metadata without a mutable dict or host callback."""

    __slots__ = ("destructive", "host_verification", "name", "scope_kind")

    def __init__(
        self,
        name: CapabilityName,
        scope_kind: str,
        *,
        destructive: bool,
        host_verification: bool,
    ) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "scope_kind", scope_kind)
        object.__setattr__(self, "destructive", destructive)
        object.__setattr__(self, "host_verification", host_verification)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CapabilitySpec is immutable")

    def __repr__(self) -> str:
        return (
            f"CapabilitySpec(name={self.name!r}, scope_kind={self.scope_kind!r}, "
            f"destructive={self.destructive!r}, host_verification={self.host_verification!r})"
        )


CAPABILITY_CATALOG: Mapping[CapabilityName, CapabilitySpec] = MappingProxyType(
    {
        CapabilityName.PATH_READ: CapabilitySpec(
            CapabilityName.PATH_READ, "path", destructive=False, host_verification=True
        ),
        CapabilityName.PATH_WRITE: CapabilitySpec(
            CapabilityName.PATH_WRITE, "path", destructive=True, host_verification=True
        ),
        CapabilityName.PATH_DELETE: CapabilitySpec(
            CapabilityName.PATH_DELETE, "path", destructive=True, host_verification=True
        ),
        CapabilityName.COMMAND_EXECUTE: CapabilitySpec(
            CapabilityName.COMMAND_EXECUTE, "command", destructive=True, host_verification=True
        ),
        CapabilityName.NETWORK_CONNECT: CapabilitySpec(
            CapabilityName.NETWORK_CONNECT, "network", destructive=True, host_verification=True
        ),
        CapabilityName.SKILL_USE: CapabilitySpec(
            CapabilityName.SKILL_USE, "skill", destructive=False, host_verification=False
        ),
        CapabilityName.ARTIFACT_PUBLISH: CapabilitySpec(
            CapabilityName.ARTIFACT_PUBLISH, "artifact", destructive=True, host_verification=True
        ),
        CapabilityName.SECRET_HANDLE_USE: CapabilitySpec(
            CapabilityName.SECRET_HANDLE_USE, "secret", destructive=True, host_verification=False
        ),
    }
)

CATALOG: Mapping[CapabilityName, CapabilitySpec] = CAPABILITY_CATALOG
_CAPABILITY_VALUES: Final[frozenset[str]] = frozenset(item.value for item in CapabilityName)
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:/|//)")


def catalog() -> tuple[CapabilitySpec, ...]:
    """Return a stable, immutable catalog view."""

    return tuple(CAPABILITY_CATALOG.values())


def capability_spec(value: CapabilityName | str) -> CapabilitySpec | None:
    try:
        return CAPABILITY_CATALOG.get(CapabilityName(value))
    except ValueError:
        return None


def default_grant_input(
    *,
    grant_id: str,
    task_id: str,
    operation_key: str,
    workspace_identity: object,
    revision: int,
    policy_revision: int,
    issued_at: datetime,
    expires_at: datetime,
    budget: AgentResourceBudget | None = None,
) -> GrantInput:
    """Construct an explicit all-deny grant with no inferred host authority."""

    return GrantInput(
        grant_id=grant_id,
        revision=revision,
        policy_revision=policy_revision,
        task_id=task_id,
        operation_key=operation_key,
        workspace_identity=workspace_identity,
        issued_at=issued_at,
        expires_at=expires_at,
        budget=budget or AgentResourceBudget(),
    )


def freeze_grant(value: GrantInput) -> GrantSnapshot:
    """Freeze authority once; later policy changes require a new snapshot."""

    return GrantSnapshot.from_input(value)


_UNBOUND_WORKSPACE_IDENTITIES = frozenset({"policy-facts", "host-verification-required"})
_UNBOUND_ROOT_IDENTITY = "host-verification-required"


def bind_verified_workspace(
    grant: GrantSnapshot,
    evidence: VerifiedWorkspaceBinding,
) -> GrantSnapshot:
    """Bind an unbound B03 snapshot to exact host-observed workspace evidence.

    This function is deliberately pure.  A file/process host obtains the
    evidence; this boundary only checks that it exactly matches the grant's
    root set and that no placeholder or ambiguous identity is accepted.
    """

    if grant.workspace_identity.identity not in _UNBOUND_WORKSPACE_IDENTITIES:
        raise ValueError("grant is already host-bound")
    if evidence.workspace_id != grant.workspace_identity.workspace_id:
        raise ValueError("workspace_id does not match grant")
    if not evidence.workspace_identity or evidence.workspace_identity in _UNBOUND_WORKSPACE_IDENTITIES:
        raise ValueError("workspace identity is missing or still a placeholder")

    expected = {(root.root_id, root.canonical_path): root for root in grant.workspace_roots}
    observed = {(root.root_id, root.canonical_path): root for root in evidence.roots}
    if len(observed) != len(evidence.roots) or expected.keys() != observed.keys():
        raise ValueError("verified root set does not exactly match grant")
    if any(root.identity != _UNBOUND_ROOT_IDENTITY for root in grant.workspace_roots):
        raise ValueError("grant root identity is not an unbound placeholder")

    bound_roots: list[WorkspaceCapability] = []
    for key, original in expected.items():
        verified = observed[key]
        if not verified.reparse_safe:
            raise ValueError("workspace root reparse proof is unsafe")
        if verified.observed_identity != verified.reparse_identity:
            raise ValueError("workspace root identity changed across reparse verification")
        if not verified.observed_identity or verified.observed_identity in _UNBOUND_WORKSPACE_IDENTITIES:
            raise ValueError("workspace root identity is missing or a placeholder")
        bound_roots.append(
            WorkspaceCapability(
                root_id=original.root_id,
                canonical_path=original.canonical_path,
                identity=verified.observed_identity,
                rights=original.rights,
            )
        )

    digest_payload = {
        "grant_id": grant.grant_id,
        "workspace_id": evidence.workspace_id,
        "workspace_identity": evidence.workspace_identity,
        "roots": [
            {
                "root_id": item.root_id,
                "canonical_path": item.canonical_path,
                "observed_identity": item.observed_identity,
                "reparse_identity": item.reparse_identity,
            }
            for item in evidence.roots
        ],
    }
    identity_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    suffix = f":verified:{identity_digest}"
    grant_id = f"{grant.grant_id[: 256 - len(suffix)]}{suffix}"
    return GrantSnapshot.model_validate(
        {
            **grant.model_dump(),
            "grant_id": grant_id,
            "workspace_identity": {
                "workspace_id": evidence.workspace_id,
                "identity": evidence.workspace_identity,
            },
            "workspace_roots": tuple(bound_roots),
        }
    )


def evaluate_capability(  # noqa: PLR0911
    grant: GrantSnapshot | None,
    request: CapabilityRequest,
    *,
    now: datetime | None = None,
    active_policy_revision: int | None = None,
) -> CapabilityDecision:
    """Evaluate a request without filesystem, process, DNS, or network access."""

    capability = request.capability.value if isinstance(request.capability, CapabilityName) else request.capability
    if capability not in _CAPABILITY_VALUES:
        return _decision(request, DenyCode.CAPABILITY_UNKNOWN, capability=capability)
    if grant is None:
        return _decision(request, DenyCode.GRANT_MISSING, capability=capability)
    if grant.task_id != request.binding.task_id or grant.operation_key != request.binding.operation_key:
        return _decision(request, DenyCode.GRANT_BINDING_MISMATCH, capability=capability, grant=grant)
    if grant.workspace_identity != request.binding.workspace_identity:
        return _decision(request, DenyCode.GRANT_BINDING_MISMATCH, capability=capability, grant=grant)
    if grant.policy_revision != request.binding.policy_revision:
        return _decision(request, DenyCode.GRANT_REVISION_MISMATCH, capability=capability, grant=grant)
    if active_policy_revision is not None and grant.policy_revision != active_policy_revision:
        return _decision(request, DenyCode.GRANT_STALE, capability=capability, grant=grant)
    check_time = (now or datetime.now(UTC)).astimezone(UTC)
    if check_time >= grant.expires_at:
        return _decision(request, DenyCode.GRANT_EXPIRED, capability=capability, grant=grant)

    try:
        name = CapabilityName(capability)
    except ValueError:
        return _decision(request, DenyCode.CAPABILITY_UNKNOWN, capability=capability, grant=grant)

    if name in {
        CapabilityName.PATH_READ,
        CapabilityName.PATH_WRITE,
        CapabilityName.PATH_DELETE,
    }:
        return _evaluate_path(grant, request, name)
    if name is CapabilityName.COMMAND_EXECUTE:
        return _evaluate_command(grant, request, name)
    if name is CapabilityName.NETWORK_CONNECT:
        return _evaluate_network(grant, request, name)
    if name is CapabilityName.SKILL_USE:
        return _evaluate_skill(grant, request, name)
    return _decision(request, DenyCode.TOOL_CAPABILITY_DENIED, capability=capability, grant=grant)


def _evaluate_path(  # noqa: PLR0911
    grant: GrantSnapshot, request: CapabilityRequest, capability: CapabilityName
) -> CapabilityDecision:
    target = request.path
    if target is None:
        return _decision(request, DenyCode.TOOL_CAPABILITY_DENIED, capability=capability, grant=grant)
    required = {
        CapabilityName.PATH_READ: PermissionRight.READ,
        CapabilityName.PATH_WRITE: PermissionRight.WRITE,
        CapabilityName.PATH_DELETE: PermissionRight.DELETE,
    }[capability]
    root = next((item for item in grant.workspace_roots if item.root_id == target.root_id), None)
    if root is None or required not in root.rights:
        return _decision(request, DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, capability=capability, grant=grant)
    if _has_parent_segment(target.path) or "\x00" in target.path:
        return _decision(request, DenyCode.TOOL_PATH_AMBIGUOUS, capability=capability, grant=grant)
    if not _is_lexically_within(root.canonical_path, target.path):
        return _decision(request, DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, capability=capability, grant=grant)
    if not target.host_verified or target.observed_identity is None:
        return _decision(
            request,
            DenyCode.HOST_VERIFICATION_REQUIRED,
            capability=capability,
            grant=grant,
            host_verification_required=True,
        )
    if target.observed_identity != root.identity:
        return _decision(request, DenyCode.TOOL_PATH_IDENTITY_CHANGED, capability=capability, grant=grant)
    return _decision(request, DenyCode.ALLOWED, capability=capability, grant=grant)


def _evaluate_command(  # noqa: PLR0911
    grant: GrantSnapshot, request: CapabilityRequest, capability: CapabilityName
) -> CapabilityDecision:
    target = request.command
    policy = grant.command
    if target is None or policy.mode == "deny" or target.shell or not target.argv:
        return _decision(request, DenyCode.TOOL_COMMAND_DENIED, capability=capability, grant=grant)
    if any(name not in policy.allowed_env_names for name in target.env_names):
        return _decision(request, DenyCode.TOOL_COMMAND_DENIED, capability=capability, grant=grant)
    executable = target.argv[0]
    if any(fnmatch.fnmatchcase(argument, pattern) for argument in target.argv for pattern in policy.denied_patterns):
        return _decision(request, DenyCode.TOOL_COMMAND_DENIED, capability=capability, grant=grant)
    if policy.mode == "explicit" and executable not in policy.allowed_executables:
        return _decision(request, DenyCode.TOOL_COMMAND_DENIED, capability=capability, grant=grant)
    if policy.mode == "workspace" and not any(
        _is_lexically_within(root.canonical_path, target.cwd) for root in grant.workspace_roots
    ):
        return _decision(request, DenyCode.TOOL_COMMAND_DENIED, capability=capability, grant=grant)
    if not target.executable_identity_verified:
        return _decision(
            request,
            DenyCode.HOST_VERIFICATION_REQUIRED,
            capability=capability,
            grant=grant,
            host_verification_required=True,
        )
    return _decision(request, DenyCode.ALLOWED, capability=capability, grant=grant)


def _evaluate_network(  # noqa: PLR0911
    grant: GrantSnapshot, request: CapabilityRequest, capability: CapabilityName
) -> CapabilityDecision:
    target = request.network
    policy = grant.network
    if target is None or policy.mode == "deny":
        return _decision(request, DenyCode.TOOL_NETWORK_DENIED, capability=capability, grant=grant)
    host = _normalise_host(target.host)
    allowed_hosts = {_normalise_host(value) for value in policy.hosts}
    if host not in allowed_hosts or target.scheme.lower() not in policy.schemes or target.port not in policy.ports:
        return _decision(request, DenyCode.TOOL_NETWORK_DENIED, capability=capability, grant=grant)
    addresses = target.resolved_addresses
    if addresses is None:
        return _decision(
            request,
            DenyCode.HOST_VERIFICATION_REQUIRED,
            capability=capability,
            grant=grant,
            host_verification_required=True,
        )
    if not policy.allow_private_addresses and any(_is_private_address(value) for value in addresses):
        return _decision(request, DenyCode.TOOL_NETWORK_DENIED, capability=capability, grant=grant)
    if target.redirect_target is not None:
        redirect = target.redirect_target
        if (
            _normalise_host(redirect.host) not in allowed_hosts
            or redirect.scheme.lower() not in policy.schemes
            or redirect.port not in policy.ports
        ):
            return _decision(request, DenyCode.TOOL_NETWORK_DENIED, capability=capability, grant=grant)
        return _decision(
            request,
            DenyCode.HOST_VERIFICATION_REQUIRED,
            capability=capability,
            grant=grant,
            host_verification_required=True,
        )
    return _decision(request, DenyCode.ALLOWED, capability=capability, grant=grant)


def _evaluate_skill(
    grant: GrantSnapshot, request: CapabilityRequest, capability: CapabilityName
) -> CapabilityDecision:
    target: SkillRequest | None = request.skill
    policy = grant.skills
    if target is None or policy.mode == "deny":
        return _decision(request, DenyCode.TOOL_SKILL_DENIED, capability=capability, grant=grant)
    if target.identity not in policy.identities or target.version not in policy.versions:
        return _decision(request, DenyCode.TOOL_SKILL_DENIED, capability=capability, grant=grant)
    return _decision(request, DenyCode.ALLOWED, capability=capability, grant=grant)


def _decision(
    request: CapabilityRequest,
    code: DenyCode,
    *,
    capability: CapabilityName | str,
    grant: GrantSnapshot | None = None,
    host_verification_required: bool = False,
) -> CapabilityDecision:
    return CapabilityDecision(
        outcome=DecisionOutcome.ALLOW if code is DenyCode.ALLOWED else DecisionOutcome.DENY,
        code=code,
        capability=capability.value if isinstance(capability, CapabilityName) else capability,
        task_id=request.binding.task_id,
        operation_key=request.binding.operation_key,
        workspace_identity=request.binding.workspace_identity,
        grant_id=grant.grant_id if grant else None,
        grant_revision=grant.revision if grant else None,
        policy_revision=grant.policy_revision if grant else request.binding.policy_revision,
        host_verification_required=host_verification_required,
    )


def _has_parent_segment(value: str) -> bool:
    return any(part == ".." for part in value.replace("\\", "/").split("/"))


def _normalise_path(value: str) -> str:
    value = value.replace("\\", "/")
    prefix = ""
    if value.startswith("//"):
        prefix, value = "//", value[2:]
    elif re.match(r"^[A-Za-z]:/", value):
        prefix, value = value[:3], value[3:]
    elif value.startswith("/"):
        prefix, value = "/", value[1:]
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        parts.append(part)
    return prefix + "/".join(parts)


def _is_lexically_within(root: str, target: str) -> bool:
    root_value = _normalise_path(root).rstrip("/") or "/"
    target_value = _normalise_path(target).rstrip("/") or "/"
    casefold = bool(_WINDOWS_PATH.match(root_value) or _WINDOWS_PATH.match(target_value))
    if casefold:
        root_value, target_value = root_value.casefold(), target_value.casefold()
    return target_value == root_value or target_value.startswith(root_value + "/")


def _normalise_host(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


__all__ = [
    "CAPABILITY_CATALOG",
    "CATALOG",
    "CATALOG_SCHEMA_VERSION",
    "CapabilitySpec",
    "capability_spec",
    "catalog",
    "default_grant_input",
    "evaluate_capability",
    "freeze_grant",
]
