from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from app.hub.client import PairingResult, SyncPushResult
from app.hub.runtime import HubRuntime
from app.hub.state import HubDevice, HubStateStore


class FakeHubClient:
    def __init__(self, _base_url: str, *, device_id: str, sync_token: str | None, timeout_s: float):
        self.device_id = device_id
        self.sync_token = sync_token

    async def create_pairing(self) -> PairingResult:
        return PairingResult(
            code="ABCD-1234",
            expires_at="2026-07-14T12:00:00Z",
            sync_token="sync-secret",
        )

    async def claim_pairing(self, _pairing_code: str):
        raise AssertionError("claim is not used by this lifecycle test")

    async def list_devices(self) -> list[HubDevice]:
        return [HubDevice(device_id=self.device_id, name="PC", is_current=True)]

    async def push(self, operations):
        return SyncPushResult(
            applied=[str(operation["operation_id"]) for operation in operations],
            duplicate=[],
            conflict=[],
        )

    async def changes(self, *, cursor, limit):
        return [], cursor or "cursor-1"

    async def snapshot(self):
        return [], "cursor-1"

    async def listen_events(self, *, cursor, stop_event, on_event):
        await stop_event.wait()

    async def revoke_device(self, _device_id: str) -> None:
        return None

    def set_sync_token(self, value: str | None) -> None:
        self.sync_token = value

    async def close(self) -> None:
        return None


class FlakyHubClient(FakeHubClient):
    attempts = 0

    async def listen_events(self, *, cursor, stop_event, on_event):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise ConnectionError("test disconnect")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)
        except TimeoutError:
            return


def _settings(state_file) -> SimpleNamespace:
    return SimpleNamespace(
        hub_enabled=True,
        hub_base_url="http://hub.test",
        hub_state_file=state_file,
        db_path=state_file.with_name("echo.db"),
        hub_request_timeout_s=2.0,
        hub_sync_interval_s=60.0,
    )


@pytest.mark.asyncio
async def test_hub_runtime_restarts_with_same_identity_and_pairing_state(monkeypatch, tmp_path):
    monkeypatch.setattr("app.hub.runtime.HubClient", FakeHubClient)
    settings = _settings(tmp_path / "hub_state.json")

    first = HubRuntime(settings)
    await first.start()
    device_id = first.state.device_id
    pairing = await first.create_pairing()
    assert pairing["pairing_code"] == "ABCD-1234"
    assert first.status()["paired"] is True
    await first.close()

    resumed = HubRuntime(settings)
    await resumed.start()
    assert resumed.status()["device_id"] == device_id
    assert resumed.status()["paired"] is True
    assert resumed.status()["pairing_code"] == "ABCD-1234"
    await resumed.close()


@pytest.mark.asyncio
async def test_hub_runtime_syncs_cursor_and_reconnects_ws(monkeypatch, tmp_path):
    monkeypatch.setattr("app.hub.runtime.HubClient", FakeHubClient)
    settings = _settings(tmp_path / "hub_state.json")
    first = HubRuntime(settings)
    await first.start()
    await first.create_pairing()
    async with first._lock:
        await first._sync_cycle_locked()
    assert first.status()["last_sync_at"] is not None
    assert first.status()["connection"] == "connected"
    await first.close()

    state = HubStateStore(settings.hub_state_file).load()
    assert state.cursor == "cursor-1"

    FlakyHubClient.attempts = 0
    monkeypatch.setattr("app.hub.runtime.HubClient", FlakyHubClient)
    resumed = HubRuntime(settings)
    await resumed.start()
    await asyncio.sleep(1.2)
    await resumed.close()
    assert FlakyHubClient.attempts >= 2
