"""Provider-neutral model protocol adapters."""

from .adapters import (
    ProviderRequest,
    build_anthropic_request,
    build_openai_compatible_request,
    normalize_anthropic_stream,
    normalize_openai_compatible_stream,
)
from .contracts import (
    MODEL_SCHEMA_VERSION,
    CanonicalMessage,
    CanonicalToolDefinition,
    ModelEventEnvelope,
    ModelRequestEnvelope,
    ModelToolRequest,
    NormalizedEvent,
    ProtocolAdapterError,
    RequestIdentity,
)

__all__ = [
    "MODEL_SCHEMA_VERSION",
    "CanonicalMessage",
    "CanonicalToolDefinition",
    "ModelEventEnvelope",
    "ModelRequestEnvelope",
    "ModelToolRequest",
    "NormalizedEvent",
    "ProtocolAdapterError",
    "ProviderRequest",
    "RequestIdentity",
    "build_anthropic_request",
    "build_openai_compatible_request",
    "normalize_anthropic_stream",
    "normalize_openai_compatible_stream",
]
