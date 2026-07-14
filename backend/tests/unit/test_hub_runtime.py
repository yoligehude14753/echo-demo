from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.hub.client import PairingResult
from app.hub.runtime import HubRuntime
from app.hub.state import HubDevice


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

    async def revoke_device(self, _device_id: str) -> None:
        return None

    def set_sync_token(self, value: str | None) -> None:
        self.sync_token = value

    async def close(self) -> None:
        return None


def _settings(state_file) -> SimpleNamespace:
    return SimpleNamespace(
        hub_enabled=True,
        hub_base_url="http://hub.test",
        hub_state_file=state_file,
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
