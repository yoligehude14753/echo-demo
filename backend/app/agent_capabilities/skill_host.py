"""Signed, bundled ``EchoSkill`` resolution and fail-closed execution.

This module owns the B06P-C skill boundary.  It never discovers a skill from
HOME or PATH, installs a runtime, starts a process, loads Claude hooks, or
executes a manifest-provided script.  A bundled skill is a signed manifest
plus hashed resources and a trusted, already-registered pure handler.  Any
side-effecting handler must use an injected B03 capability host of its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .catalog import evaluate_capability
from .redaction import redact_text
from .types import (
    CapabilityDecision,
    CapabilityName,
    CapabilityRequest,
    DenyCode,
    FrozenModel,
    GrantSnapshot,
    InvocationBinding,
    SkillRequest,
)

UNSUPPORTED_P0_FAIL_CLOSED: Final[str] = "UNSUPPORTED_P0_FAIL_CLOSED"
SKILL_MANIFEST_SIGNATURE_INVALID: Final[str] = "SKILL_MANIFEST_SIGNATURE_INVALID"
SKILL_RESOURCE_HASH_MISMATCH: Final[str] = "SKILL_RESOURCE_HASH_MISMATCH"
SKILL_RESOURCE_OUTSIDE_BUNDLE: Final[str] = "SKILL_RESOURCE_OUTSIDE_BUNDLE"
SKILL_RESOURCE_NOT_FOUND: Final[str] = "SKILL_RESOURCE_NOT_FOUND"
SKILL_HANDLER_NOT_REGISTERED: Final[str] = "SKILL_HANDLER_NOT_REGISTERED"
SKILL_HANDLER_FAILED: Final[str] = "SKILL_HANDLER_FAILED"
SKILL_CAPABILITY_DEFERRED: Final[str] = "SKILL_CAPABILITY_DEFERRED"
SKILL_MANIFEST_INVALID: Final[str] = "SKILL_MANIFEST_INVALID"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTRYPOINT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_P0_SCAN_RE = re.compile(
    r"(?ix)"
    r"(?:\bclaude(?:\s+-p|\s+code)?\b|"
    r"(?:^|[^.\w])\.claude(?:[^.\w]|$)|"
    r"\b(?:npm|pnpm|yarn|pip)\s+install\b|"
    r"\b(?:HOME|PATH)\b|process\.env\.(?:HOME|PATH)|os\.environ|"
    r"(?:^|[\W_])hooks?(?:[\W_]|$))"
)


def _safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError("identifier must be a safe non-empty identifier")
    return value


def _safe_provenance(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("provenance must be non-empty and value-free")
    if redact_text(value) != value or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError("provenance contains a secret or unsafe value")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ManifestSignatureVerifier(Protocol):
    """Trust boundary for the release signer; no key discovery is allowed."""

    def verify(self, payload: bytes, signature: str, signer_id: str) -> bool:
        """Return true only when the explicit signer authenticates payload."""


class HmacSha256ManifestVerifier:
    """Small stdlib verifier for tests and controlled embedded bundles.

    Production callers must inject keys explicitly.  The host never reads a
    key from environment variables, HOME, a config file, or a global store.
    """

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        self._keys = MappingProxyType(dict(keys))

    def verify(self, payload: bytes, signature: str, signer_id: str) -> bool:
        key = self._keys.get(signer_id)
        if key is None or not isinstance(key, bytes) or not key:
            return False
        if not signature.startswith("hmac-sha256:"):
            return False
        encoded = signature.split(":", 1)[1]
        try:
            supplied = base64.b16decode(encoded.upper(), casefold=True)
        except ValueError:
            return False
        expected = hmac.new(key, payload, hashlib.sha256).digest()
        return hmac.compare_digest(supplied, expected)


class SkillResource(FrozenModel):
    """A manifest resource addressed only by a normalized POSIX path."""

    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        parsed = PurePosixPath(value)
        if value.startswith(("/", "\\")) or "\\" in value or ".." in parsed.parts:
            raise ValueError("resource path must be a relative POSIX path")
        if not value or value == "." or any(part in {"", "."} for part in parsed.parts):
            raise ValueError("resource path is ambiguous")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("resource sha256 must be a lowercase hex digest")
        return value


class SkillManifest(FrozenModel):
    """Signed identity and resource contract for one bundled EchoSkill."""

    schema_version: Literal[1] = 1
    identity: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    entrypoint: str = Field(min_length=2, max_length=128)
    required_capabilities: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("any",)
    resources: tuple[SkillResource, ...] = ()
    provenance: str = Field(min_length=1, max_length=256)
    signer_id: str = Field(min_length=1, max_length=256)
    signature: str = Field(min_length=1, max_length=1024)

    _identity = field_validator("identity", "version", "signer_id")(_safe_identifier)
    _provenance = field_validator("provenance")(_safe_provenance)

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        if not _ENTRYPOINT_RE.fullmatch(value):
            raise ValueError("entrypoint must name a registered embedded handler")
        return value

    @field_validator("required_capabilities", "platforms", mode="before")
    @classmethod
    def _safe_values(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or value is None:
            raise ValueError("manifest values must be a sequence")
        values = tuple(value)  # type: ignore[arg-type]
        if any(not isinstance(item, str) or not item for item in values):
            raise ValueError("manifest values must be non-empty strings")
        return values

    @model_validator(mode="after")
    def _requires_skill_capability(self) -> SkillManifest:
        if CapabilityName.SKILL_USE.value not in self.required_capabilities:
            raise ValueError("bundled skill must declare skill.use")
        if not self.platforms or any(item not in {"any", "macos", "windows", "linux"} for item in self.platforms):
            raise ValueError("manifest platform is unsupported")
        return self

    def signed_payload(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))

    @property
    def manifest_sha256(self) -> str:
        return _sha256(self.signed_payload())


@dataclass(frozen=True)
class ResolvedSkill:
    manifest: SkillManifest
    manifest_sha256: str
    resource_hashes: tuple[str, ...]


class SkillResolutionError(ValueError):
    """A deterministic, non-secret manifest or bundle rejection."""

    def __init__(self, code: str, message: str = "skill bundle rejected") -> None:
        super().__init__(message)
        self.code = code


class SkillResolver:
    """Resolve only resources below an explicit, caller-owned bundle root."""

    def __init__(self, bundle_root: Path, verifier: ManifestSignatureVerifier, *, platform: str) -> None:
        self._root = bundle_root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("bundle root must be a directory")
        self._verifier = verifier
        self._platform = platform.lower()

    def resolve(self, manifest: SkillManifest) -> ResolvedSkill:
        self._verify_signature(manifest)
        if "any" not in manifest.platforms and self._platform not in manifest.platforms:
            raise SkillResolutionError(UNSUPPORTED_P0_FAIL_CLOSED, "skill platform is not bundled")
        resource_hashes: list[str] = []
        for resource in manifest.resources:
            resource_hashes.append(self._verify_resource(resource))
        return ResolvedSkill(manifest, manifest.manifest_sha256, tuple(resource_hashes))

    def _verify_signature(self, manifest: SkillManifest) -> None:
        try:
            trusted = self._verifier.verify(
                manifest.signed_payload(), manifest.signature, manifest.signer_id
            )
        except Exception as exc:  # verifier is an external trust boundary
            raise SkillResolutionError(SKILL_MANIFEST_SIGNATURE_INVALID) from exc
        if not trusted:
            raise SkillResolutionError(SKILL_MANIFEST_SIGNATURE_INVALID)

    def _verify_resource(self, resource: SkillResource) -> str:
        raw = self._root / resource.path
        self._reject_symlink_components(raw)
        try:
            candidate = raw.resolve(strict=True)
        except OSError as exc:
            raise SkillResolutionError(SKILL_RESOURCE_NOT_FOUND) from exc
        if not self._within_root(candidate) or not candidate.is_file():
            raise SkillResolutionError(SKILL_RESOURCE_OUTSIDE_BUNDLE)
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise SkillResolutionError(SKILL_RESOURCE_NOT_FOUND) from exc
        digest = _sha256(content)
        if digest != resource.sha256:
            raise SkillResolutionError(SKILL_RESOURCE_HASH_MISMATCH)
        if _P0_SCAN_RE.search(content.decode("utf-8", errors="ignore")):
            raise SkillResolutionError(UNSUPPORTED_P0_FAIL_CLOSED)
        return f"sha256:{digest}"

    def _within_root(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return False
        return True

    def _reject_symlink_components(self, path: Path) -> None:
        relative = path.relative_to(self._root)
        current = self._root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise SkillResolutionError(SKILL_RESOURCE_OUTSIDE_BUNDLE)


class SkillExecutionContext:
    """Metadata and B03-only authorization helper exposed to a skill handler."""

    def __init__(self, *, grant: GrantSnapshot, tool_use_id: str) -> None:
        self.grant = grant
        self.task_id = grant.task_id
        self.operation_key = grant.operation_key
        self.tool_use_id = tool_use_id

    def authorize(self, request: CapabilityRequest, *, now: datetime | None = None) -> CapabilityDecision:
        """Delegate every additional capability decision to the B03 source."""

        return evaluate_capability(self.grant, request, now=now)


SkillHandler = Callable[[Mapping[str, Any], SkillExecutionContext], Mapping[str, Any]]


class SkillReceipt(FrozenModel):
    """Value-free operation receipt with explicit skill provenance."""

    schema_version: Literal[1] = 1
    event_type: str = "capability.operation.receipt"
    receipt_id: str
    occurred_at: datetime
    operation: str = "skill.invoke"
    outcome: str
    result: str
    code: str
    capability: str = CapabilityName.SKILL_USE.value
    task_id: str
    operation_key: str
    tool_use_id: str
    grant_id: str | None
    grant_revision: int | None
    policy_revision: int | None
    skill_identity: str
    skill_version: str
    manifest_sha256: str
    resource_hashes: tuple[str, ...]
    provenance: str
    signer_id: str
    input_sha256: str | None = None
    output_sha256: str | None = None
    redacted: bool = True

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt timestamp must include timezone")
        return value.astimezone(UTC)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class SkillResult:
    value: Mapping[str, Any] | None
    decision: CapabilityDecision
    receipt: SkillReceipt

    @property
    def ok(self) -> bool:
        return self.receipt.result == "succeeded"


class EchoSkillHost:
    """Invoke only a verified manifest and a pre-registered embedded handler."""

    def __init__(self, resolver: SkillResolver, handlers: Mapping[str, SkillHandler]) -> None:
        self._resolver = resolver
        self._handlers = MappingProxyType(dict(handlers))

    def invoke(
        self,
        *,
        manifest: SkillManifest,
        payload: Mapping[str, Any],
        grant: GrantSnapshot,
        request: CapabilityRequest,
        tool_use_id: str,
        grant_revision: int,
        current_grant: Callable[[], GrantSnapshot | None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        active_policy_revision: int | None = None,
        now: datetime | None = None,
    ) -> SkillResult:
        started_at = now or datetime.now(UTC)
        decision = self._authorize(
            manifest=manifest,
            grant=grant,
            request=request,
            tool_use_id=tool_use_id,
            grant_revision=grant_revision,
            current_grant=current_grant,
            is_cancelled=is_cancelled,
            active_policy_revision=active_policy_revision,
            now=started_at,
        )
        if not decision.allowed:
            return self._result(
                manifest,
                decision,
                "denied",
                decision.code.value,
                tool_use_id,
                started_at,
            )
        try:
            resolved = self._resolver.resolve(manifest)
        except SkillResolutionError as exc:
            denied = self._deny(decision, DenyCode.TOOL_SKILL_DENIED)
            return self._result(manifest, denied, "denied", exc.code, tool_use_id, started_at)
        if any(item != CapabilityName.SKILL_USE.value for item in manifest.required_capabilities):
            denied = self._deny(decision, DenyCode.TOOL_SKILL_DENIED)
            return self._result(manifest, denied, "denied", SKILL_CAPABILITY_DEFERRED, tool_use_id, started_at, resolved)
        handler = self._handlers.get(manifest.entrypoint)
        if handler is None:
            denied = self._deny(decision, DenyCode.TOOL_SKILL_DENIED)
            return self._result(manifest, denied, "denied", SKILL_HANDLER_NOT_REGISTERED, tool_use_id, started_at, resolved)
        if not isinstance(payload, Mapping):
            denied = self._deny(decision, DenyCode.TOOL_SKILL_DENIED)
            return self._result(manifest, denied, "denied", SKILL_MANIFEST_INVALID, tool_use_id, started_at, resolved)
        return self._run_handler(
            handler,
            manifest=manifest,
            resolved=resolved,
            payload=payload,
            grant=grant,
            decision=decision,
            tool_use_id=tool_use_id,
            current_grant=current_grant,
            is_cancelled=is_cancelled,
            started_at=started_at,
        )

    def _authorize(  # noqa: PLR0911
        self,
        *,
        manifest: SkillManifest,
        grant: GrantSnapshot,
        request: CapabilityRequest,
        tool_use_id: str,
        grant_revision: int,
        current_grant: Callable[[], GrantSnapshot | None] | None,
        is_cancelled: Callable[[], bool] | None,
        active_policy_revision: int | None,
        now: datetime,
    ) -> CapabilityDecision:
        if not isinstance(tool_use_id, str) or not _SAFE_ID_RE.fullmatch(tool_use_id):
            return self._deny_for(request, grant, DenyCode.TOOL_CAPABILITY_DENIED)
        current = current_grant() if current_grant is not None else grant
        if current is None or (is_cancelled is not None and is_cancelled()):
            return self._deny_for(request, grant, DenyCode.GRANT_REVOKED)
        if grant_revision != grant.revision or current.revision != grant_revision:
            return self._deny_for(request, grant, DenyCode.GRANT_REVISION_MISMATCH)
        if current != grant:
            return self._deny_for(request, grant, DenyCode.GRANT_BINDING_MISMATCH)
        if request.capability != CapabilityName.SKILL_USE:
            return self._deny_for(request, grant, DenyCode.CAPABILITY_UNKNOWN)
        if request.skill != SkillRequest(identity=manifest.identity, version=manifest.version):
            return self._deny_for(request, grant, DenyCode.TOOL_SKILL_DENIED)
        if request.binding != InvocationBinding(
            task_id=grant.task_id,
            operation_key=grant.operation_key,
            workspace_identity=grant.workspace_identity,
            policy_revision=grant.policy_revision,
        ):
            return self._deny_for(request, grant, DenyCode.GRANT_BINDING_MISMATCH)
        return evaluate_capability(
            grant,
            request,
            now=now,
            active_policy_revision=active_policy_revision,
        )

    def _run_handler(
        self,
        handler: SkillHandler,
        *,
        manifest: SkillManifest,
        resolved: ResolvedSkill,
        payload: Mapping[str, Any],
        grant: GrantSnapshot,
        decision: CapabilityDecision,
        tool_use_id: str,
        current_grant: Callable[[], GrantSnapshot | None] | None,
        is_cancelled: Callable[[], bool] | None,
        started_at: datetime,
    ) -> SkillResult:
        try:
            output = handler(payload, SkillExecutionContext(grant=grant, tool_use_id=tool_use_id))
            output_bytes = _canonical_json(output)
        except Exception:
            return self._result(manifest, decision, "failed", SKILL_HANDLER_FAILED, tool_use_id, started_at, resolved, payload)
        current = current_grant() if current_grant is not None else grant
        if current is None or current != grant or (is_cancelled is not None and is_cancelled()):
            denied = self._deny(decision, DenyCode.GRANT_REVOKED)
            return self._result(manifest, denied, "denied", DenyCode.GRANT_REVOKED.value, tool_use_id, started_at, resolved, payload)
        return self._result(manifest, decision, "succeeded", "ALLOWED", tool_use_id, started_at, resolved, payload, output_bytes, output)

    def _result(
        self,
        manifest: SkillManifest,
        decision: CapabilityDecision,
        result: str,
        code: str,
        tool_use_id: str,
        occurred_at: datetime,
        resolved: ResolvedSkill | None = None,
        payload: Mapping[str, Any] | None = None,
        output_bytes: bytes | None = None,
        output: Mapping[str, Any] | None = None,
    ) -> SkillResult:
        manifest_hash = resolved.manifest_sha256 if resolved else manifest.manifest_sha256
        resource_hashes = resolved.resource_hashes if resolved else tuple(
            f"sha256:{resource.sha256}" for resource in manifest.resources
        )
        identity = ":".join((manifest.identity, manifest.version, tool_use_id, manifest_hash, code))
        receipt = SkillReceipt(
            receipt_id="skill-receipt-" + _sha256(identity.encode("utf-8"))[:32],
            occurred_at=occurred_at.astimezone(UTC),
            outcome="allow" if decision.allowed else "deny",
            result=result,
            code=code,
            task_id=decision.task_id,
            operation_key=decision.operation_key,
            tool_use_id=tool_use_id,
            grant_id=decision.grant_id,
            grant_revision=decision.grant_revision,
            policy_revision=decision.policy_revision,
            skill_identity=manifest.identity,
            skill_version=manifest.version,
            manifest_sha256=f"sha256:{manifest_hash}",
            resource_hashes=resource_hashes,
            provenance=manifest.provenance,
            signer_id=manifest.signer_id,
            input_sha256=_sha256(_canonical_json(payload)) if payload is not None else None,
            output_sha256=_sha256(output_bytes) if output_bytes is not None else None,
        )
        return SkillResult(output, decision, receipt)

    @staticmethod
    def _deny(decision: CapabilityDecision, code: DenyCode) -> CapabilityDecision:
        return decision.model_copy(update={"outcome": "deny", "code": code})

    @staticmethod
    def _deny_for(request: CapabilityRequest, grant: GrantSnapshot, code: DenyCode) -> CapabilityDecision:
        return CapabilityDecision(
            outcome="deny",
            code=code,
            capability=CapabilityName.SKILL_USE.value,
            task_id=request.binding.task_id,
            operation_key=request.binding.operation_key,
            workspace_identity=grant.workspace_identity,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            policy_revision=grant.policy_revision,
        )


__all__ = [
    "SKILL_CAPABILITY_DEFERRED",
    "SKILL_HANDLER_FAILED",
    "SKILL_HANDLER_NOT_REGISTERED",
    "SKILL_MANIFEST_INVALID",
    "SKILL_MANIFEST_SIGNATURE_INVALID",
    "SKILL_RESOURCE_HASH_MISMATCH",
    "SKILL_RESOURCE_NOT_FOUND",
    "SKILL_RESOURCE_OUTSIDE_BUNDLE",
    "UNSUPPORTED_P0_FAIL_CLOSED",
    "EchoSkillHost",
    "HmacSha256ManifestVerifier",
    "ManifestSignatureVerifier",
    "ResolvedSkill",
    "SkillExecutionContext",
    "SkillHandler",
    "SkillManifest",
    "SkillReceipt",
    "SkillResolutionError",
    "SkillResolver",
    "SkillResource",
    "SkillResult",
]
