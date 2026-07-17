"""Pure capability and grant value objects.

The models in this module deliberately contain no host handles and perform no
I/O.  They are the immutable input/output boundary between Echo's authority
source and a capability host.  A host must verify filesystem, process, DNS,
redirect, and reparse-point facts again before performing a side effect.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    """A recursively immutable Pydantic boundary model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class CapabilityName(StrEnum):
    PATH_READ = "path.read"
    PATH_WRITE = "path.write"
    PATH_DELETE = "path.delete"
    COMMAND_EXECUTE = "command.execute"
    NETWORK_CONNECT = "network.connect"
    SKILL_USE = "skill.use"
    ARTIFACT_PUBLISH = "artifact.publish"
    SECRET_HANDLE_USE = "secret.handle.use"


class ScopeKind(StrEnum):
    PATH = "path"
    COMMAND = "command"
    NETWORK = "network"
    SKILL = "skill"
    ARTIFACT = "artifact"
    SECRET = "secret"


class PermissionRight(StrEnum):
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"


class CapabilityMode(StrEnum):
    DENY = "deny"
    WORKSPACE = "workspace"
    EXPLICIT = "explicit"
    ALLOWLIST = "allowlist"


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class DenyCode(StrEnum):
    """Stable, non-secret permission decision codes."""

    ALLOWED = "ALLOWED"
    GRANT_MISSING = "GRANT_MISSING"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    GRANT_REVOKED = "GRANT_REVOKED"
    GRANT_REVISION_MISMATCH = "GRANT_REVISION_MISMATCH"
    GRANT_STALE = "GRANT_STALE"
    GRANT_BINDING_MISMATCH = "GRANT_BINDING_MISMATCH"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CAPABILITY_SCOPE_CONFLICT = "CAPABILITY_SCOPE_CONFLICT"
    TOOL_CAPABILITY_DENIED = "TOOL_CAPABILITY_DENIED"
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    TOOL_PATH_OUTSIDE_WORKSPACE = "TOOL_PATH_OUTSIDE_WORKSPACE"
    TOOL_PATH_IDENTITY_CHANGED = "TOOL_PATH_IDENTITY_CHANGED"
    TOOL_PATH_AMBIGUOUS = "TOOL_PATH_AMBIGUOUS"
    TOOL_COMMAND_DENIED = "TOOL_COMMAND_DENIED"
    TOOL_NETWORK_DENIED = "TOOL_NETWORK_DENIED"
    TOOL_SKILL_DENIED = "TOOL_SKILL_DENIED"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    HOST_VERIFICATION_REQUIRED = "HOST_VERIFICATION_REQUIRED"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def _non_blank(value: str) -> str:
    if not value:
        raise ValueError("value must not be blank")
    if "\x00" in value:
        raise ValueError("value must not contain NUL")
    return value


def _validate_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError("value must be a sequence of strings")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError("values must be non-empty strings without NUL")
        values.append(item)
    return tuple(values)


def _validate_optional_string_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _validate_string_tuple(value)


class WorkspaceIdentity(FrozenModel):
    """The server-authored identity used to bind an invocation to a workspace."""

    workspace_id: str = Field(min_length=1, max_length=256)
    identity: str = Field(min_length=1, max_length=256)

    _validate_workspace_id = field_validator("workspace_id", "identity")(_non_blank)


class WorkspaceCapability(FrozenModel):
    """A lexical workspace root; ``identity`` is verified by the host."""

    root_id: str = Field(min_length=1, max_length=256)
    canonical_path: str = Field(min_length=1, max_length=4096)
    identity: str = Field(min_length=1, max_length=256)
    rights: tuple[PermissionRight, ...] = ()

    _validate_values = field_validator("root_id", "canonical_path", "identity")(_non_blank)


class VerifiedWorkspaceRoot(FrozenModel):
    """Host-produced, value-only proof for one grant workspace root."""

    root_id: str = Field(min_length=1, max_length=256)
    canonical_path: str = Field(min_length=1, max_length=4096)
    observed_identity: str = Field(min_length=1, max_length=256)
    reparse_identity: str = Field(min_length=1, max_length=256)
    reparse_safe: bool = True

    _validate_values = field_validator(
        "root_id", "canonical_path", "observed_identity", "reparse_identity"
    )(_non_blank)


class CommandCapability(FrozenModel):
    mode: Literal["deny", "workspace", "explicit"] = "deny"
    allowed_executables: tuple[str, ...] = ()
    denied_patterns: tuple[str, ...] = ()
    allowed_env_names: tuple[str, ...] = ()
    max_wall_seconds: int = Field(default=1, ge=1, le=7200)
    max_output_bytes: int = Field(default=1, ge=1, le=67_108_864)

    _validate_values = field_validator(
        "allowed_executables", "denied_patterns", "allowed_env_names", mode="before"
    )(_validate_string_tuple)


class NetworkCapability(FrozenModel):
    mode: Literal["deny", "allowlist"] = "deny"
    hosts: tuple[str, ...] = ()
    schemes: tuple[Literal["http", "https"], ...] = ()
    ports: tuple[int, ...] = ()
    allow_private_addresses: bool = False

    _validate_values = field_validator("hosts", mode="before")(_validate_string_tuple)

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(port < 1 or port > 65_535 for port in value):
            raise ValueError("ports must be between 1 and 65535")
        return tuple(sorted(set(value)))


class SkillCapability(FrozenModel):
    mode: Literal["deny", "allowlist"] = "deny"
    identities: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()

    _validate_values = field_validator("identities", "versions", mode="before")(
        _validate_string_tuple
    )


class ArtifactCapability(FrozenModel):
    mode: Literal["deny", "allowlist"] = "deny"
    max_bytes: int = Field(default=1, ge=1, le=524_288_000)


class SecretCapability(FrozenModel):
    """Only opaque server-side handles may cross this boundary."""

    mode: Literal["deny", "allowlist"] = "deny"
    handles: tuple[str, ...] = ()

    _validate_values = field_validator("handles", mode="before")(_validate_string_tuple)


class AgentResourceBudget(FrozenModel):
    wall_seconds: int = Field(default=1800, ge=1, le=7200)
    max_turns: int = Field(default=200, ge=1, le=1000)
    max_tool_calls: int = Field(default=500, ge=1, le=5000)
    max_model_input_tokens: int = Field(default=1, ge=1)
    max_model_output_tokens: int = Field(default=1, ge=1)
    max_tool_output_bytes: int = Field(default=67_108_864, ge=1, le=67_108_864)
    max_artifact_bytes: int = Field(default=524_288_000, ge=1, le=524_288_000)
    max_concurrent_tools: int = Field(default=4, ge=1, le=16)


class GrantInput(FrozenModel):
    """Authority-authored grant input, before freezing into a task snapshot."""

    grant_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    workspace_identity: WorkspaceIdentity
    issued_at: datetime
    expires_at: datetime
    workspace_roots: tuple[WorkspaceCapability, ...] = ()
    command: CommandCapability = Field(default_factory=CommandCapability)
    network: NetworkCapability = Field(default_factory=NetworkCapability)
    artifacts: ArtifactCapability = Field(default_factory=ArtifactCapability)
    secrets: SecretCapability = Field(default_factory=SecretCapability)
    skills: SkillCapability = Field(default_factory=SkillCapability)
    budget: AgentResourceBudget = Field(default_factory=AgentResourceBudget)

    _validate_ids = field_validator("grant_id", "task_id", "operation_key")(_non_blank)
    _validate_timestamps = field_validator("issued_at", "expires_at")(_aware_utc)

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> GrantInput:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self


class GrantSnapshot(GrantInput):
    """Immutable, task-bound capability authority for one session."""

    schema_version: Literal[1] = 1

    @classmethod
    def from_input(cls, value: GrantInput) -> GrantSnapshot:
        return cls.model_validate(value.model_dump())


class VerifiedWorkspaceBinding(FrozenModel):
    """Pure host evidence used to bind an unbound grant to real roots."""

    workspace_id: str = Field(min_length=1, max_length=256)
    workspace_identity: str = Field(min_length=1, max_length=256)
    roots: tuple[VerifiedWorkspaceRoot, ...] = ()

    _validate_values = field_validator("workspace_id", "workspace_identity")(_non_blank)


class InvocationBinding(FrozenModel):
    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    workspace_identity: WorkspaceIdentity
    policy_revision: int = Field(ge=1)

    _validate_ids = field_validator("task_id", "operation_key")(_non_blank)


class PathRequest(FrozenModel):
    path: str = Field(min_length=1, max_length=4096)
    root_id: str = Field(min_length=1, max_length=256)
    right: PermissionRight
    host_verified: bool = False
    observed_identity: str | None = None

    _validate_values = field_validator("path", "root_id")(_non_blank)


class CommandRequest(FrozenModel):
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1, max_length=4096)
    env_names: tuple[str, ...] = ()
    shell: bool = False
    executable_identity_verified: bool = False

    _validate_argv = field_validator("argv", mode="before")(_validate_string_tuple)
    _validate_env = field_validator("env_names", mode="before")(_validate_string_tuple)
    _validate_cwd = field_validator("cwd")(_non_blank)


class NetworkRequest(FrozenModel):
    scheme: str = Field(min_length=1, max_length=16)
    host: str = Field(min_length=1, max_length=512)
    port: int = Field(ge=1, le=65_535)
    resolved_addresses: tuple[str, ...] | None = None
    redirect_target: NetworkRequest | None = None

    _validate_values = field_validator("scheme", "host")(_non_blank)
    _validate_resolved = field_validator("resolved_addresses", mode="before")(
        _validate_optional_string_tuple
    )


class SkillRequest(FrozenModel):
    identity: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)

    _validate_values = field_validator("identity", "version")(_non_blank)


class CapabilityRequest(FrozenModel):
    """Typed invocation envelope; no raw command/env/secret values are accepted."""

    capability: CapabilityName | str
    binding: InvocationBinding
    path: PathRequest | None = None
    command: CommandRequest | None = None
    network: NetworkRequest | None = None
    skill: SkillRequest | None = None

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: CapabilityName | str) -> CapabilityName | str:
        return _non_blank(value.value if isinstance(value, CapabilityName) else value)


class CapabilityDecision(FrozenModel):
    """Stable pure-policy result, intentionally without free-text details."""

    outcome: DecisionOutcome
    code: DenyCode
    capability: str
    task_id: str
    operation_key: str
    workspace_identity: WorkspaceIdentity
    grant_id: str | None = None
    grant_revision: int | None = None
    policy_revision: int | None = None
    host_verification_required: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW


CapabilityScope: TypeAlias = (
    WorkspaceCapability
    | CommandCapability
    | NetworkCapability
    | SkillCapability
    | ArtifactCapability
    | SecretCapability
)

CATALOG_SCHEMA_VERSION: Final[int] = 1

# Contract-facing aliases keep the names used by the architecture document
# available without introducing a second model hierarchy.
PathScope = WorkspaceCapability
CommandScope = CommandCapability
NetworkScope = NetworkCapability
SkillScope = SkillCapability


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "AgentResourceBudget",
    "ArtifactCapability",
    "CapabilityDecision",
    "CapabilityMode",
    "CapabilityName",
    "CapabilityRequest",
    "CapabilityScope",
    "CommandCapability",
    "CommandRequest",
    "CommandScope",
    "DecisionOutcome",
    "DenyCode",
    "FrozenModel",
    "GrantInput",
    "GrantSnapshot",
    "InvocationBinding",
    "NetworkCapability",
    "NetworkRequest",
    "NetworkScope",
    "PathRequest",
    "PathScope",
    "PermissionRight",
    "ScopeKind",
    "SecretCapability",
    "SkillCapability",
    "SkillRequest",
    "SkillScope",
    "VerifiedWorkspaceBinding",
    "VerifiedWorkspaceRoot",
    "WorkspaceCapability",
    "WorkspaceIdentity",
]
