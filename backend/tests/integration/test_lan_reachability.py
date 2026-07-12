from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import get_repository
from app.config import Settings, get_settings
from app.main import create_app


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        return int(listener.getsockname()[1])


def _lan_address() -> str:
    # UDP connect selects the host's routable interface without sending data.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])


@pytest.mark.integration
async def test_bind_all_is_reachable_from_second_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    lan_address = _lan_address()
    assert not lan_address.startswith("127.")
    settings = Settings(
        db_path=tmp_path / "lan.db",
        storage_dir=tmp_path / "storage",
        port=port,
        public_http_url=f"http://{lan_address}:{port}",
        public_ws_url=f"ws://{lan_address}:{port}/ws/echo",
        _env_file=None,  # type: ignore[call-arg]
    )
    repository = SQLiteRepository(settings.db_path)
    await repository.init()
    await repository.create_meeting(
        "lan-share",
        started_at=datetime.now(UTC),
        title="LAN share smoke",
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_repository] = lambda: repository
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        lifespan="off",
        ws_max_size=4096,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(_wait_started(server), timeout=10)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get(f"http://{lan_address}:{port}/healthz")
            blocked = await client.get(f"http://{lan_address}:{port}/bootstrap")
            shared = await client.get(f"http://{lan_address}:{port}/meetings/lan-share/share")
        assert response.status_code == 200
        assert blocked.status_code == 403
        assert shared.status_code == 200
        assert "LAN share smoke" in shared.text
        assert settings.public_http_url == f"http://{lan_address}:{port}"
        assert settings.public_ws_url == f"ws://{lan_address}:{port}/ws/echo"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        await repository.aclose()


async def _wait_started(server: uvicorn.Server) -> None:
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes only a boolean startup flag
        await asyncio.sleep(0.01)
