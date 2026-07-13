"""EchoDesk layered memory service.

Public callers depend on the scoped service/models only.  Persistence remains
an internal detail so tenant/owner isolation cannot be bypassed accidentally.
"""

from .models import MemoryScope, RecallResult
from .service import MemoryService, aclose_memory_service, get_memory_service

__all__ = [
    "MemoryScope",
    "MemoryService",
    "RecallResult",
    "aclose_memory_service",
    "get_memory_service",
]
