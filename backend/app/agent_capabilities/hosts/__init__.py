"""B06P file and mutation capability hosts.

This package deliberately has no registry or invocation dispatch.  The public
catalog, immutable grant, and pure policy remain owned by B03; these modules
only perform the final host verification immediately around filesystem I/O.
"""

from .common import HostContext, HostResult, OperationReceipt, ToolInvocation
from .file import FileReadHost, GrepMatch
from .mutation import AtomicMutationHost
from .paths import PathVerifier, VerifiedPath

__all__ = [
    "AtomicMutationHost",
    "FileReadHost",
    "GrepMatch",
    "HostContext",
    "HostResult",
    "OperationReceipt",
    "PathVerifier",
    "ToolInvocation",
    "VerifiedPath",
]
