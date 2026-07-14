from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import Settings
from app.main import create_app
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    PUBLIC_CLIENT_VERSION_HEADER,
)
from fastapi.testclient import TestClient


def _settings(tmp_path: Path, *, public: bool) -> Settings:
    return Settings(
        db_path=tmp_path / "sync.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=public,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    settings = _settings(tmp_path, public=True)
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    deps_mod.reset_deps_for_test()
    with TestClient(
        app,
        headers={PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION},
    ) as client:
        yield client
    deps_mod.reset_deps_for_test()


def _enrollment(label: str) -> dict[str, str]:
    return {
        "enrollment_id": f"enrollment-{label}-" + "e" * 40,
        "device_secret": f"device-secret-{label}-" + "s" * 40,
    }


@pytest.mark.unit
def test_pairing_claim_list_revoke_and_one_time_code(
    public_client: TestClient,
) -> None:
    enrollment = _enrollment("owner-a")
    session = public_client.post("/session", json=enrollment)
    assert session.status_code == 201, session.text
    access_token = session.json()["token"]

    pairing = public_client.post(
        "/hub/v1/pairings",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"ttl_seconds": 300},
    )
    assert pairing.status_code == 201, pairing.text
    pairing_body = pairing.json()
    assert pairing_body["pairing_code"].startswith("pair_")

    claim = public_client.post(
        "/hub/v1/pairings/claim",
        json={
            "pairing_code": pairing_body["pairing_code"],
            "device_id": "desktop-device-a",
            "device_name": "Desktop",
            "platform": "desktop",
        },
    )
    assert claim.status_code == 200, claim.text
    claim_body = claim.json()
    assert claim_body["device_id"] == "desktop-device-a"
    assert claim_body["sync_token"].startswith("sync_")
    assert claim_body["cursor"] == 0

    duplicate_claim = public_client.post(
        "/hub/v1/pairings/claim",
        json={
            "pairing_code": pairing_body["pairing_code"],
            "device_id": "desktop-device-b",
            "device_name": "Desktop 2",
            "platform": "desktop",
        },
    )
    assert duplicate_claim.status_code == 404

    sync_headers = {"X-Echo-Sync-Token": claim_body["sync_token"]}
    devices = public_client.get("/hub/v1/devices", headers=sync_headers)
    assert devices.status_code == 200, devices.text
    assert [item["device_id"] for item in devices.json()] == ["desktop-device-a"]

    revoked = public_client.delete(
        "/hub/v1/devices/desktop-device-a",
        headers=sync_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    after_revoke = public_client.get("/hub/v1/devices", headers=sync_headers)
    assert after_revoke.status_code == 401


@pytest.mark.unit
def test_pairing_and_sync_tokens_are_stored_only_as_hashes(
    public_client: TestClient,
    tmp_path: Path,
) -> None:
    enrollment = _enrollment("hashes")
    session = public_client.post("/session", json=enrollment).json()
    pairing = public_client.post(
        "/hub/v1/pairings",
        headers={"Authorization": f"Bearer {session['token']}"},
    ).json()
    claim = public_client.post(
        "/hub/v1/pairings/claim",
        json={
            "pairing_code": pairing["pairing_code"],
            "device_id": "hash-device",
            "device_name": "Hash test",
            "platform": "test",
        },
    ).json()

    async def read_rows() -> tuple[str, str]:
        async with aiosqlite.connect(str(tmp_path / "sync.db")) as conn:
            pairing_row = await (
                await conn.execute(
                    "SELECT pairing_code_hash FROM device_pairings WHERE pairing_id = ?",
                    (pairing["pairing_id"],),
                )
            ).fetchone()
            device_row = await (
                await conn.execute(
                    "SELECT sync_token_hash FROM sync_devices WHERE device_id = ?",
                    (claim["device_id"],),
                )
            ).fetchone()
        assert pairing_row is not None
        assert device_row is not None
        return str(pairing_row[0]), str(device_row[0])

    pairing_hash, sync_hash = asyncio.run(read_rows())
    assert pairing_hash == hashlib.sha256(pairing["pairing_code"].encode()).hexdigest()
    assert sync_hash == hashlib.sha256(claim["sync_token"].encode()).hexdigest()
    assert pairing["pairing_code"] not in pairing_hash
    assert claim["sync_token"] not in sync_hash
