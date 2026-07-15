from __future__ import annotations

import json
import stat

from app.hub.state import HubStateStore


def test_hub_state_survives_restart_without_exposing_token(tmp_path):
    state_path = tmp_path / "nested" / "hub_state.json"
    store = HubStateStore(state_path)

    first = store.load()
    first.sync_token = "sync-secret"
    first.cursor = "cursor-7"
    first.pairing_code = "ABCD-1234"
    first.pairing_expires_at = "2026-07-14T12:00:00Z"
    store.save(first)

    resumed = HubStateStore(state_path).load()

    assert resumed.device_id == first.device_id
    assert resumed.sync_token == "sync-secret"
    assert resumed.cursor == "cursor-7"
    assert resumed.pairing_code == "ABCD-1234"
    public = resumed.public_payload(enabled=True, configured=True)
    assert public["device_id"] == first.device_id
    assert public["paired"] is True
    assert "sync_token" not in public
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_hub_state_file_is_atomic_json(tmp_path):
    state_path = tmp_path / "hub_state.json"
    store = HubStateStore(state_path)
    state = store.load()

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["device_id"] == state.device_id
    assert state_path.parent.exists()
    assert not list(state_path.parent.glob("*.tmp"))
