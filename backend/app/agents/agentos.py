"""Compatibility exports for the embedded runtime cutover.

The historical module name is retained so existing imports do not become a
second control plane.  The implementation is exclusively the inherited-fd
Electron runtime adapter; it does not contain an HTTP/WebSocket/CLI fallback.
"""

from app.agents.embedded_runtime import (
    AGENTOS_SUBMIT_MAX_WALL_S,
    EmbeddedRuntimeBackend,
    EmbeddedRuntimeError,
    submit_operation_key,
)

AgentOSBackend = EmbeddedRuntimeBackend

__all__ = [
    "AGENTOS_SUBMIT_MAX_WALL_S",
    "AgentOSBackend",
    "EmbeddedRuntimeBackend",
    "EmbeddedRuntimeError",
    "submit_operation_key",
]
