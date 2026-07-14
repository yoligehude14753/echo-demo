"""Persistent local state for the desktop Hub client.

The Hub client deliberately keeps its state in the existing EchoDesk user
configuration directory.  The file is written atomically and is restricted
to the current user; no custom cryptography is involved in the local store.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


HubConnectionState = Literal[
    "disabled",
    "pairing_required",
    "connecting",
    "connected",
    "disconnected",
    "error",
]

_CONNECTION_STATES: set[str] = {
    "disabled",
    "pairing_required",
    "connecting",
    "connected",
    "disconnected",
    "error",
}


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(slots=True)
class HubDevice:
    """The non-secret device fields shown in the desktop settings UI."""

    device_id: str
    name: str | None = None
    platform: str | None = None
    status: str | None = None
    is_current: bool = False
    last_seen_at: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> HubDevice | None:
        if not isinstance(payload, dict):
            return None
        device_id = _string(
            payload.get("device_id")
            or payload.get("deviceId")
            or payload.get("id")
        )
        if not device_id:
            return None
        status = _string(payload.get("status"))
        if status is None and isinstance(payload.get("online"), bool):
            status = "online" if payload["online"] else "offline"
        return cls(
            device_id=device_id,
            name=_string(payload.get("name") or payload.get("device_name")),
            platform=_string(payload.get("platform")),
            status=status,
            is_current=bool(payload.get("is_current") or payload.get("isCurrent")),
            last_seen_at=_string(
                payload.get("last_seen_at") or payload.get("lastSeenAt")
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "platform": self.platform,
            "status": self.status,
            "is_current": self.is_current,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(slots=True)
class HubState:
    """Recoverable local state for one desktop installation."""

    schema: int = 1
    device_id: str = field(default_factory=lambda: str(uuid4()))
    sync_token: str | None = None
    cursor: str | None = None
    pairing_code: str | None = None
    pairing_expires_at: str | None = None
    connection: HubConnectionState = "disconnected"
    last_sync_at: str | None = None
    last_connected_at: str | None = None
    last_error: str | None = None
    devices: list[HubDevice] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> HubState:
        if not isinstance(payload, dict):
            return cls()

        connection = _string(payload.get("connection"))
        if connection not in _CONNECTION_STATES:
            connection = "disconnected"

        raw_devices = payload.get("devices")
        devices: list[HubDevice] = []
        if isinstance(raw_devices, list):
            for raw_device in raw_devices:
                device = HubDevice.from_payload(raw_device)
                if device is not None:
                    devices.append(device)

        raw_schema = payload.get("schema")
        schema = raw_schema if isinstance(raw_schema, int) and raw_schema > 0 else 1
        return cls(
            schema=schema,
            device_id=_string(payload.get("device_id")) or str(uuid4()),
            sync_token=_string(payload.get("sync_token")),
            cursor=_string(payload.get("cursor")),
            pairing_code=_string(payload.get("pairing_code")),
            pairing_expires_at=_string(payload.get("pairing_expires_at")),
            connection=connection,  # type: ignore[arg-type]
            last_sync_at=_string(payload.get("last_sync_at")),
            last_connected_at=_string(payload.get("last_connected_at")),
            last_error=_string(payload.get("last_error")),
            devices=devices,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "device_id": self.device_id,
            "sync_token": self.sync_token,
            "cursor": self.cursor,
            "pairing_code": self.pairing_code,
            "pairing_expires_at": self.pairing_expires_at,
            "connection": self.connection,
            "last_sync_at": self.last_sync_at,
            "last_connected_at": self.last_connected_at,
            "last_error": self.last_error,
            "devices": [device.to_payload() for device in self.devices],
        }

    def public_payload(self, *, enabled: bool, configured: bool) -> dict[str, Any]:
        """Return only fields safe for the local settings API."""

        return {
            "enabled": enabled,
            "configured": configured,
            "device_id": self.device_id,
            "paired": bool(self.sync_token),
            "connection": self.connection,
            "pairing_code": self.pairing_code,
            "pairing_expires_at": self.pairing_expires_at,
            "devices": [device.to_payload() for device in self.devices],
            "last_sync_at": self.last_sync_at,
            "last_connected_at": self.last_connected_at,
            "last_error": self.last_error,
        }


class HubStateError(RuntimeError):
    """Raised when the local Hub state cannot be read or written."""


class HubStateStore:
    """Read and atomically write :class:`HubState` JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> HubState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = HubState()
            self.save(state)
            return state
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HubStateError("unable to read Hub state") from exc
        return HubState.from_payload(payload)

    def save(self, state: HubState) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=parent,
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
                    json.dump(
                        state.to_payload(),
                        temporary_file,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    temporary_file.write("\n")
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_name, self.path)
                os.chmod(self.path, 0o600)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            raise HubStateError("unable to write Hub state") from exc
