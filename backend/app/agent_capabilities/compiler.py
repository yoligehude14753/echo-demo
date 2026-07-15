"""Compile Echo permission facts into immutable, host-independent grants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .catalog import freeze_grant
from .policy import (
    SUPPORTED_CAPABILITIES,
    CapabilityFact,
    CommandScope,
    Decision,
    DecisionStatus,
    NetworkScope,
    NetworkTarget,
    PathScope,
    PermissionFacts,
    PolicyInputError,
    ReasonCode,
    SkillScope,
    canonical_capability,
    command_is_authorized,
    network_is_authorized,
    normalize_command_scope,
    normalize_network_request,
    normalize_network_scope,
    normalize_path_request,
    normalize_path_scope,
    normalize_skill_scope,
    path_is_within_root,
    skill_is_authorized,
)
from .types import (
    CommandCapability,
    GrantInput,
    GrantSnapshot,
    NetworkCapability,
    PermissionRight,
    SkillCapability,
    WorkspaceCapability,
    WorkspaceIdentity,
)


@dataclass(frozen=True)
class CompiledCapability:
    capability: str
    effect: str
    scope: object


ImmutableGrant = GrantSnapshot


@dataclass(frozen=True)
class CompileResult:
    decision: Decision
    grant: GrantSnapshot | None = None
    rules: tuple[CompiledCapability, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision.allowed and self.grant is not None

    @property
    def status(self) -> DecisionStatus:
        return self.decision.status

    @property
    def reason_code(self) -> ReasonCode:
        return self.decision.reason_code


def compile_grant(  # noqa: PLR0911, PLR0912
    facts: PermissionFacts | Mapping[str, Any],
    *,
    task_id: str,
    expected_revision: int | None = None,
    now: datetime | None = None,
    requested_capabilities: tuple[str, ...] | None = None,
) -> CompileResult:
    """Compile a grant without reading time, filesystem, environment, or network.

    ``now`` is intentionally explicit whenever facts carry an expiry.  Omitting
    it cannot accidentally turn an expired or freshness-unknown fact into an
    allow decision.
    """

    try:
        normalized_facts = _normalize_facts(facts)
        _validate_task_id(task_id)
        if normalized_facts.revision <= 0:
            return _failure(ReasonCode.STALE_REVISION, "grant revision must be positive")
        if expected_revision is not None and normalized_facts.revision != expected_revision:
            return _failure(ReasonCode.STALE_REVISION, "grant revision does not match the expected revision")
        if normalized_facts.stale:
            return _failure(ReasonCode.STALE_REVISION, "permission facts are marked stale")
        expires_at = normalized_facts.expires_at
        if expires_at is not None:
            if now is None:
                return _failure(ReasonCode.REVISION_FRESHNESS_UNVERIFIED, "explicit now is required for expiring facts")
            checked_now = _normalize_now(now)
            if checked_now >= expires_at:
                return _failure(ReasonCode.EXPIRED_REVISION, "permission facts have expired")
        required = _normalize_requested_capabilities(requested_capabilities)
        compiled: list[CompiledCapability] = []
        for fact in normalized_facts.capabilities:
            capability = canonical_capability(fact.capability)
            if capability not in SUPPORTED_CAPABILITIES:
                return _failure(ReasonCode.UNKNOWN_CAPABILITY, f"unsupported capability: {capability}")
            if required is not None and capability not in required:
                continue
            scope = _normalize_scope(capability, fact.scope)
            compiled.append(CompiledCapability(capability, _normalize_effect(fact.effect), scope))
            if capability == "network.connect" and _network_needs_host_verification(scope):
                return CompileResult(
                    Decision(
                        DecisionStatus.HOST_VERIFICATION_REQUIRED,
                        ReasonCode.AMBIGUOUS_INPUT,
                        capability,
                        "network grant requires caller-supplied public resolution evidence",
                    )
                )
        conflict = _find_conflict(compiled)
        if conflict is not None:
            return _failure(ReasonCode.CONFLICTING_SCOPE, conflict)
        immutable_capabilities = tuple(
            sorted(compiled, key=lambda item: (item.capability, item.effect, _canonical(item.scope)))
        )
        grant = _public_grant(normalized_facts, task_id, immutable_capabilities, expires_at=expires_at)
        return CompileResult(
            Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, detail="immutable grant compiled"),
            grant,
            immutable_capabilities,
        )
    except PolicyInputError as exc:
        return _failure(exc.reason_code, exc.detail)
    except (KeyError, TypeError, ValueError) as exc:
        return _failure(ReasonCode.INVALID_INPUT, str(exc))


def decide(  # noqa: PLR0911
    source: CompileResult | GrantSnapshot | PermissionFacts | Mapping[str, Any],
    capability: str,
    request: object,
    *,
    expected_revision: int | None = None,
    now: datetime | None = None,
    task_id: str = "decision-only",
) -> Decision:
    """Evaluate one normalized request against facts or an immutable grant."""

    try:
        canonical = canonical_capability(capability)
    except PolicyInputError as exc:
        return _failure(exc.reason_code, exc.detail).decision
    if isinstance(source, CompileResult):
        if not source.allowed or source.grant is None:
            return source.decision
        grant = source.grant
        rules = source.rules
        freshness = _check_grant_freshness(grant, expected_revision=expected_revision, now=now)
        if freshness is not None:
            return freshness
    elif isinstance(source, GrantSnapshot):
        grant = source
        freshness = _check_grant_freshness(grant, expected_revision=expected_revision, now=now)
        if freshness is not None:
            return freshness
        return _failure(ReasonCode.INVALID_INPUT, "direct public snapshots require typed catalog requests").decision
    else:
        result = compile_grant(source, task_id=task_id, expected_revision=expected_revision, now=now)
        if not result.allowed:
            return result.decision
        assert result.grant is not None
        grant = result.grant
        rules = result.rules
    return _decide_with_rules(grant, rules, canonical, request)


def decide_against_grant(grant: GrantSnapshot, capability: str, request: object) -> Decision:
    return _failure(ReasonCode.INVALID_INPUT, "direct public snapshots require typed catalog requests").decision


def _decide_with_rules(
    grant: GrantSnapshot,
    rules: tuple[CompiledCapability, ...],
    capability: str,
    request: object,
) -> Decision:
    try:
        canonical = canonical_capability(capability)
        normalized_request = _normalize_request(canonical, request)
    except PolicyInputError as exc:
        return _failure(exc.reason_code, exc.detail, capability=capability).decision
    rules = [item for item in rules if item.capability == canonical]
    if not rules:
        return _failure(_reason_for_capability(canonical), "no matching capability in immutable grant", capability=canonical).decision
    for rule in rules:
        if rule.effect == "deny" and _scope_matches(canonical, rule.scope, normalized_request):
            return _failure(_reason_for_capability(canonical), "explicit deny rule matched", capability=canonical).decision
    failed_match: Decision | None = None
    for rule in rules:
        if rule.effect != "allow":
            continue
        decision = _evaluate_rule(canonical, rule.scope, normalized_request)
        if decision.allowed:
            return decision
        if decision.host_verification_required:
            return decision
        failed_match = decision
    return failed_match or _failure(_reason_for_capability(canonical), "request is outside immutable grant scope", capability=canonical).decision


def _normalize_facts(facts: PermissionFacts | Mapping[str, Any]) -> PermissionFacts:
    if isinstance(facts, PermissionFacts):
        return facts
    if not isinstance(facts, Mapping):
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "permission facts must be a mapping or PermissionFacts")
    raw_capabilities = facts.get("capabilities", facts.get("facts", ()))
    capabilities: list[CapabilityFact] = []
    for raw in raw_capabilities:
        if isinstance(raw, CapabilityFact):
            capabilities.append(raw)
        elif isinstance(raw, Mapping):
            capabilities.append(CapabilityFact(str(raw.get("capability", "")), raw.get("scope", {}), str(raw.get("effect", "allow"))))
        else:
            raise PolicyInputError(ReasonCode.INVALID_INPUT, "each permission fact must be a mapping")
    return PermissionFacts(
        revision=int(facts.get("revision", 0)),
        capabilities=tuple(capabilities),
        issued_at=facts.get("issued_at"),
        expires_at=facts.get("expires_at"),
        stale=bool(facts.get("stale", False)),
    )


def _normalize_scope(capability: str, scope: object) -> object:
    if capability in {"path.read", "path.write"}:
        return normalize_path_scope(scope)  # type: ignore[arg-type]
    if capability == "command.execute":
        if isinstance(scope, CommandScope):
            return normalize_command_scope(scope.argv, cwd=scope.cwd, env_names=scope.env_names, platform=scope.platform)
        if not isinstance(scope, Mapping):
            raise PolicyInputError(ReasonCode.INVALID_INPUT, "command scope must be a mapping")
        return normalize_command_scope(
            scope.get("argv", ()),
            cwd=scope.get("cwd", ""),
            env_names=scope.get("env_names", ()),
            platform=scope.get("platform", ""),
        )
    if capability == "network.connect":
        return normalize_network_scope(scope)  # type: ignore[arg-type]
    if capability == "skill.use":
        return normalize_skill_scope(scope)  # type: ignore[arg-type]
    raise PolicyInputError(ReasonCode.UNKNOWN_CAPABILITY, f"no scope normalizer for {capability}")


def _normalize_request(capability: str, request: object) -> object:
    if capability in {"path.read", "path.write"}:
        if isinstance(request, Mapping):
            return normalize_path_request(request.get("path", ""), platform=request.get("platform", ""))
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "path request needs path and platform")
    if capability == "command.execute":
        if isinstance(request, CommandScope):
            return normalize_command_scope(request.argv, cwd=request.cwd, env_names=request.env_names, platform=request.platform)
        if isinstance(request, Mapping):
            return normalize_command_scope(
                request.get("argv", ()),
                cwd=request.get("cwd", ""),
                env_names=request.get("env_names", ()),
                platform=request.get("platform", ""),
            )
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "command request must be argv-based")
    if capability == "network.connect":
        return normalize_network_request(request)  # type: ignore[arg-type]
    if capability == "skill.use":
        return normalize_skill_scope(request)  # type: ignore[arg-type]
    raise PolicyInputError(ReasonCode.UNKNOWN_CAPABILITY, f"no request normalizer for {capability}")


def _evaluate_rule(capability: str, scope: object, request: object) -> Decision:  # noqa: PLR0911
    if capability in {"path.read", "path.write"}:
        assert isinstance(scope, PathScope)
        assert hasattr(request, "path")
        if any(path_is_within_root(root, request) for root in scope.roots):
            return Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, capability, normalized=request)
        return _failure(ReasonCode.PATH_OUTSIDE_ROOT, "path is outside every granted root", capability=capability).decision
    if capability == "command.execute":
        assert isinstance(scope, CommandScope) and isinstance(request, CommandScope)
        if command_is_authorized(scope, request):
            return Decision(DecisionStatus.ALLOW, ReasonCode.ALLOWED, capability, normalized=request)
        return _failure(ReasonCode.COMMAND_NOT_AUTHORIZED, "argv, cwd, or env names differ", capability=capability).decision
    if capability == "network.connect":
        assert isinstance(scope, NetworkScope)
        return network_is_authorized(scope, request)  # type: ignore[arg-type]
    if capability == "skill.use":
        assert isinstance(scope, SkillScope) and isinstance(request, SkillScope)
        return skill_is_authorized(scope, request)
    return _failure(ReasonCode.UNKNOWN_CAPABILITY, "unsupported capability", capability=capability).decision


def _scope_matches(capability: str, scope: object, request: object) -> bool:
    result = _evaluate_rule(capability, scope, request)
    return result.allowed


def _find_conflict(capabilities: list[CompiledCapability]) -> str | None:
    for index, left in enumerate(capabilities):
        for right in capabilities[index + 1 :]:
            if left.capability != right.capability or left.effect == right.effect:
                continue
            if _scopes_overlap(left.capability, left.scope, right.scope):
                return f"conflicting {left.capability} scopes have effects {left.effect!r} and {right.effect!r}"
    return None


def _scopes_overlap(capability: str, left: object, right: object) -> bool:
    if capability in {"path.read", "path.write"}:
        assert isinstance(left, PathScope) and isinstance(right, PathScope)
        return any(path_is_within_root(a, b.root) or path_is_within_root(b, a.root) for a in left.roots for b in right.roots)
    if capability == "command.execute":
        assert isinstance(left, CommandScope) and isinstance(right, CommandScope)
        return left.platform == right.platform and left.argv == right.argv and left.cwd == right.cwd and bool(set(left.env_names) & set(right.env_names) or not left.env_names or not right.env_names)
    if capability == "network.connect":
        assert isinstance(left, NetworkScope) and isinstance(right, NetworkScope)
        return left.target == right.target
    if capability == "skill.use":
        assert isinstance(left, SkillScope) and isinstance(right, SkillScope)
        return left.identity == right.identity and left.version == right.version
    return True


def _network_needs_host_verification(scope: object) -> bool:
    assert isinstance(scope, NetworkScope)
    return scope.target.host_verification_required or any(item.host_verification_required for item in scope.allowed_redirects)


def _normalize_effect(effect: str) -> str:
    if effect not in {"allow", "deny"}:
        raise PolicyInputError(ReasonCode.CONFLICTING_SCOPE, "effect must be allow or deny")
    return effect


def _normalize_requested_capabilities(values: tuple[str, ...] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(canonical_capability(value) for value in values)


def _check_grant_freshness(grant: GrantSnapshot, *, expected_revision: int | None, now: datetime | None) -> Decision | None:
    if expected_revision is not None and grant.grant_revision != expected_revision:
        return _failure(ReasonCode.STALE_REVISION, "grant revision does not match the expected revision").decision
    if grant.expires_at is not None:
        if now is None:
            return _failure(ReasonCode.REVISION_FRESHNESS_UNVERIFIED, "explicit now is required for expiring grants").decision
        if _normalize_now(now) >= grant.expires_at:
            return _failure(ReasonCode.EXPIRED_REVISION, "grant has expired").decision
    return None


def _normalize_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise PolicyInputError(ReasonCode.AMBIGUOUS_INPUT, "now needs an explicit timezone")
    return now.astimezone(UTC)


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not task_id or any(char in task_id for char in "\r\n\x00"):
        raise PolicyInputError(ReasonCode.INVALID_INPUT, "task_id is not canonical")


def _reason_for_capability(capability: str) -> ReasonCode:
    if capability in {"path.read", "path.write"}:
        return ReasonCode.PATH_OUTSIDE_ROOT
    if capability == "command.execute":
        return ReasonCode.COMMAND_NOT_AUTHORIZED
    if capability == "network.connect":
        return ReasonCode.NETWORK_NOT_AUTHORIZED
    if capability == "skill.use":
        return ReasonCode.SKILL_NOT_AUTHORIZED
    return ReasonCode.UNKNOWN_CAPABILITY


def _failure(reason: ReasonCode, detail: str, *, capability: str | None = None) -> CompileResult:
    return CompileResult(Decision(DecisionStatus.DENY, reason, capability, detail))


def _canonical(value: object) -> str:
    if isinstance(value, PathScope):
        return json.dumps({"roots": [(item.platform, item.root) for item in value.roots]}, separators=(",", ":"))
    if isinstance(value, CommandScope):
        return json.dumps({"platform": value.platform, "argv": value.argv, "cwd": value.cwd, "env_names": value.env_names}, separators=(",", ":"))
    if isinstance(value, NetworkScope):
        return json.dumps({"target": _canonical(value.target), "redirects": [_canonical(item) for item in value.allowed_redirects]}, separators=(",", ":"))
    if isinstance(value, NetworkTarget):
        return json.dumps((value.scheme, value.host, value.port, value.verified_ips), separators=(",", ":"))
    if isinstance(value, SkillScope):
        return json.dumps((value.identity, value.version, value.provenance), separators=(",", ":"))
    return repr(value)


def _grant_id(facts: PermissionFacts, task_id: str, capabilities: tuple[CompiledCapability, ...]) -> str:
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "revision": facts.revision,
        "expires_at": facts.expires_at.isoformat() if facts.expires_at else None,
        "capabilities": [(item.capability, item.effect, _canonical(item.scope)) for item in capabilities],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "grant_" + hashlib.sha256(encoded).hexdigest()[:32]


def _public_grant(
    facts: PermissionFacts,
    task_id: str,
    capabilities: tuple[CompiledCapability, ...],
    *,
    expires_at: datetime | None,
) -> GrantSnapshot:
    """Project B03 rules into the one public Grant v1 snapshot model."""

    roots: dict[tuple[str, str], set[PermissionRight]] = {}
    commands: list[CommandScope] = []
    networks: list[NetworkScope] = []
    skills: list[SkillScope] = []
    for item in capabilities:
        if item.effect != "allow":
            continue
        if item.capability in {"path.read", "path.write"}:
            right = PermissionRight.READ if item.capability == "path.read" else PermissionRight.WRITE
            assert isinstance(item.scope, PathScope)
            for root in item.scope.roots:
                roots.setdefault((root.platform, root.root), set()).add(right)
        elif item.capability == "command.execute":
            assert isinstance(item.scope, CommandScope)
            commands.append(item.scope)
        elif item.capability == "network.connect":
            assert isinstance(item.scope, NetworkScope)
            networks.append(item.scope)
        elif item.capability == "skill.use":
            assert isinstance(item.scope, SkillScope)
            skills.append(item.scope)

    workspace_roots = tuple(
        WorkspaceCapability(
            root_id="root_" + hashlib.sha256(f"{platform}:{root}".encode()).hexdigest()[:16],
            canonical_path=root,
            identity="host-verification-required",
            rights=tuple(sorted(rights, key=lambda value: value.value)),
        )
        for (platform, root), rights in sorted(roots.items())
    )
    allowed_executables = tuple(sorted({scope.argv[0] for scope in commands}))
    allowed_env_names = tuple(sorted({name for scope in commands for name in scope.env_names}))
    network_hosts = tuple(sorted({scope.target.host for scope in networks}))
    network_schemes = tuple(sorted({scope.target.scheme for scope in networks}))
    network_ports = tuple(sorted({scope.target.port for scope in networks}))
    skill_identities = tuple(sorted({scope.identity for scope in skills}))
    skill_versions = tuple(sorted({scope.version for scope in skills}))
    issued_at = facts.issued_at or datetime(1970, 1, 1, tzinfo=UTC)
    effective_expires_at = expires_at or datetime.max.replace(tzinfo=UTC)
    value = GrantInput(
        grant_id=_grant_id(facts, task_id, capabilities),
        revision=facts.revision,
        policy_revision=facts.revision,
        task_id=task_id,
        operation_key=f"{task_id}:capability-policy",
        workspace_identity=WorkspaceIdentity(workspace_id=task_id, identity="policy-facts"),
        issued_at=issued_at,
        expires_at=effective_expires_at,
        workspace_roots=workspace_roots,
        command=CommandCapability(
            mode="explicit" if allowed_executables else "deny",
            allowed_executables=allowed_executables,
            allowed_env_names=allowed_env_names,
        ),
        network=NetworkCapability(
            mode="allowlist" if network_hosts else "deny",
            hosts=network_hosts,
            schemes=network_schemes,
            ports=network_ports,
        ),
        skills=SkillCapability(
            mode="allowlist" if skill_identities else "deny",
            identities=skill_identities,
            versions=skill_versions,
        ),
    )
    return freeze_grant(value)
