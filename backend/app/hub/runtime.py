"""Hub lifecycle owned by the existing EchoDesk backend process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

from .client import HubClient, HubError
from .state import HubState, HubStateError, HubStateStore

logger = logging.getLogger("echodesk.hub")


class HubRuntimeError(RuntimeError):
    """A user-safe operation failure exposed by the local Hub API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


class HubRuntime:
    """Own the Hub client, reconnect loop, and recoverable local state."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = HubStateStore(settings.hub_state_file)
        self.state = HubState()
        self._client: HubClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.hub_base_url.strip())

    async def start(self) -> None:
        try:
            self.state = self.store.load()
        except HubStateError:
            # A malformed local state must not prevent the existing desktop
            # backend from starting.  Recreate only the Hub state namespace.
            logger.warning("Hub state recovery failed; creating a new local identity")
            self.state = HubState()
            self._persist_safely()

        if not self.settings.hub_enabled:
            self.state.connection = "disabled"
            self._persist_safely()
            return

        if not self.configured:
            self.state.connection = "disconnected"
            self._persist_safely()
            return

        try:
            self._client = HubClient(
                self.settings.hub_base_url,
                device_id=self.state.device_id,
                sync_token=self.state.sync_token,
                timeout_s=self.settings.hub_request_timeout_s,
            )
        except HubError:
            self.state.connection = "error"
            self.state.last_error = "connection_failed"
            self._persist_safely()
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._connection_loop(), name="hub-runtime")

    async def close(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    def status(self) -> dict[str, Any]:
        return self.state.public_payload(
            enabled=self.settings.hub_enabled,
            configured=self.configured,
        )

    async def create_pairing(self) -> dict[str, Any]:
        async with self._lock:
            client = self._require_client("pairing_failed")
            self.state.connection = "connecting"
            self.state.last_error = None
            self._persist_safely()
            try:
                result = await client.create_pairing()
            except HubError as exc:
                self._mark_error("pairing_failed")
                raise HubRuntimeError("pairing_failed") from exc

            self.state.pairing_code = result.code
            self.state.pairing_expires_at = result.expires_at
            if result.sync_token:
                self.state.sync_token = result.sync_token
                client.set_sync_token(result.sync_token)
            self.state.connection = "connected" if self.state.sync_token else "pairing_required"
            self.state.last_error = None
            self._persist_or_raise()
            return {
                "pairing_code": result.code,
                "expires_at": result.expires_at,
            }

    async def claim_pairing(self, pairing_code: str) -> None:
        async with self._lock:
            client = self._require_client("pairing_failed")
            try:
                result = await client.claim_pairing(pairing_code)
            except HubError as exc:
                self._mark_error("pairing_failed")
                raise HubRuntimeError("pairing_failed") from exc
            if not result.sync_token:
                self._mark_error("pairing_failed")
                raise HubRuntimeError("pairing_failed")
            self.state.sync_token = result.sync_token
            self.state.pairing_code = None
            self.state.pairing_expires_at = None
            self.state.connection = "disconnected"
            self.state.last_error = None
            client.set_sync_token(result.sync_token)
            self._persist_or_raise()

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                device.to_payload()
                for device in await self._list_devices_locked()
            ]

    async def revoke_device(self, device_id: str) -> None:
        async with self._lock:
            client = self._require_client("connection_failed")
            try:
                await client.revoke_device(device_id)
            except HubError as exc:
                self._mark_error("connection_failed")
                raise HubRuntimeError("connection_failed") from exc
            self.state.devices = [
                device
                for device in self.state.devices
                if device.device_id != device_id
            ]
            self.state.last_error = None
            self._persist_or_raise()

    async def _connection_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            if self.state.sync_token and self._client is not None:
                async with self._lock:
                    try:
                        await self._list_devices_locked()
                    except HubRuntimeError:
                        pass
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.hub_sync_interval_s,
                )
            except asyncio.TimeoutError:
                continue

    async def _list_devices_locked(self) -> list[Any]:
        client = self._require_client("connection_failed")
        if not self.state.sync_token:
            self.state.connection = "pairing_required"
            self._persist_safely()
            return []
        try:
            devices = await client.list_devices()
        except HubError as exc:
            self._mark_error("connection_failed")
            raise HubRuntimeError("connection_failed") from exc
        self.state.devices = devices
        self.state.connection = "connected"
        self.state.last_connected_at = _now()
        self.state.last_error = None
        self._persist_safely()
        return devices

    def _require_client(self, error_code: str) -> HubClient:
        if not self.settings.hub_enabled or not self.configured or self._client is None:
            self._mark_error(error_code)
            raise HubRuntimeError(error_code)
        return self._client

    def _mark_error(self, error_code: str) -> None:
        self.state.connection = "error"
        self.state.last_error = error_code
        self._persist_safely()

    def _persist_or_raise(self) -> None:
        try:
            self.store.save(self.state)
        except HubStateError as exc:
            logger.warning("Hub state persistence failed")
            raise HubRuntimeError("sync_failed") from exc

    def _persist_safely(self) -> None:
        try:
            self.store.save(self.state)
        except HubStateError:
            logger.warning("Hub state persistence failed")
