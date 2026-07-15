"""Pure model-runtime contracts and protocol adapters for Echo."""

from .config import (
    canonical_config_hash,
    compile_model_runtime_config,
    compile_model_runtime_snapshot,
    compile_snapshot,
    normalize_model_runtime_config,
)
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
    "MODEL_RUNTIME_SCHEMA_VERSION",
    "ModelCapabilities",
    "ModelLimits",
    "ModelRoute",
    "ModelRuntimeConfig",
    "ModelRuntimeSnapshot",
    "ReasoningPolicy",
    "RequestIdentity",
    "TokenizerPolicy",
    "assert_snapshot_revision",
    "canonical_config_hash",
    "compile_model_runtime_config",
    "compile_model_runtime_snapshot",
    "compile_snapshot",
    "normalize_model_runtime_config",
    "validate_request_identity",
]
