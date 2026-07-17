"""Pure model-runtime contracts and protocol adapters for Echo."""

from .config import (
    canonical_config_hash,
    compile_model_runtime_config,
    compile_model_runtime_snapshot,
    compile_route_snapshot,
    compile_snapshot,
    normalize_model_runtime_config,
)
from .config_store import (
    MODEL_RUNTIME_CONFIG_KEY,
    ModelRuntimeConfigStore,
    migrate_model_runtime_config,
)
from .credentials import (
    CredentialHandle,
    CredentialHandleError,
    CredentialResolver,
    InMemoryCredentialResolver,
    ResolvedCredential,
    validate_credential_handle,
)
from .fallback import (
    ExplicitFallbackRouter,
    FallbackDecision,
    ModelFallbackEvent,
)
from .health import (
    CapabilityProbeResult,
    ModelHealthChecker,
    RouteHealthReport,
    safe_model_diagnostic,
)
from .revision import TaskModelBinding, TaskModelRevisionRegistry
from .snapshot import ModelRuntimeSnapshot, assert_snapshot_revision, validate_request_identity
from .types import (
    MODEL_RUNTIME_SCHEMA_VERSION,
    ModelCapabilities,
    ModelLimits,
    ModelRoute,
    ModelRuntimeConfig,
    ReasoningPolicy,
    RequestIdentity,
    TokenizerPolicy,
)

__all__ = [
    "MODEL_RUNTIME_CONFIG_KEY",
    "MODEL_RUNTIME_SCHEMA_VERSION",
    "CapabilityProbeResult",
    "CredentialHandle",
    "CredentialHandleError",
    "CredentialResolver",
    "ExplicitFallbackRouter",
    "FallbackDecision",
    "InMemoryCredentialResolver",
    "ModelCapabilities",
    "ModelFallbackEvent",
    "ModelHealthChecker",
    "ModelLimits",
    "ModelRoute",
    "ModelRuntimeConfig",
    "ModelRuntimeConfigStore",
    "ModelRuntimeSnapshot",
    "ReasoningPolicy",
    "RequestIdentity",
    "ResolvedCredential",
    "RouteHealthReport",
    "TaskModelBinding",
    "TaskModelRevisionRegistry",
    "TokenizerPolicy",
    "assert_snapshot_revision",
    "canonical_config_hash",
    "compile_model_runtime_config",
    "compile_model_runtime_snapshot",
    "compile_route_snapshot",
    "compile_snapshot",
    "migrate_model_runtime_config",
    "normalize_model_runtime_config",
    "safe_model_diagnostic",
    "validate_credential_handle",
    "validate_request_identity",
]
