"""Same-user multi-device synchronization primitives."""

from .store import (
    ClaimedDevice,
    DeviceAlreadyExistsError,
    PairingNotFoundError,
    PairingRecord,
    PushResult,
    SnapshotResult,
    SyncChangeRecord,
    SyncDeviceNotFoundError,
    SyncDeviceRecord,
    SyncEntityValidationError,
    SyncHubStore,
)

__all__ = [
    "ClaimedDevice",
    "DeviceAlreadyExistsError",
    "PairingNotFoundError",
    "PairingRecord",
    "PushResult",
    "SnapshotResult",
    "SyncChangeRecord",
    "SyncDeviceNotFoundError",
    "SyncDeviceRecord",
    "SyncEntityValidationError",
    "SyncHubStore",
]
