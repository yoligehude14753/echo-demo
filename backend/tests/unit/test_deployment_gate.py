from __future__ import annotations

import os
import runpy
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from app.security.deployment_gate import DeploymentGateMiddleware
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "a" * 64
HEADER = "X-Echo-Deployment-Gate"


def _gate_file(tmp_path: Path) -> Path:
    gate = tmp_path / "deployment-gate.token"
    gate.write_text(f"{TOKEN}\n", encoding="ascii")
    gate.chmod(0o600)
    return gate


def _client(gate: Path) -> TestClient:
    inner = FastAPI()

    @inner.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @inner.get("/business")
    async def business() -> dict[str, bool]:
        return {"ok": True}

    @inner.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    return TestClient(DeploymentGateMiddleware(inner, gate_file=gate))


@pytest.mark.unit
def test_closed_gate_allows_probes_and_token_smoke_but_blocks_public_business(
    tmp_path: Path,
) -> None:
    gate = _gate_file(tmp_path)
    with _client(gate) as client:
        assert client.get("/healthz").status_code == 200
        blocked = client.get("/business")
        assert blocked.status_code == 503
        assert blocked.headers["retry-after"] == "5"
        assert TOKEN not in blocked.text
        assert client.get("/business", headers={HEADER: "wrong"}).status_code == 503
        assert client.get("/business", headers={HEADER: TOKEN}).json() == {"ok": True}

        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("/ws"):
            pass
        assert closed.value.code == 1013
        with client.websocket_connect("/ws", headers={HEADER: TOKEN}) as websocket:
            assert websocket.receive_text() == "ok"


@pytest.mark.unit
def test_gate_file_removal_opens_and_malformed_or_symlinked_files_fail_closed(
    tmp_path: Path,
) -> None:
    gate = _gate_file(tmp_path)
    with _client(gate) as client:
        gate.unlink()
        assert client.get("/business").status_code == 200

        gate.write_text(f"{TOKEN}\n", encoding="ascii")
        gate.chmod(0o644)
        assert client.get("/business", headers={HEADER: TOKEN}).status_code == 503

        gate.unlink()
        target = tmp_path / "target.token"
        target.write_text(f"{TOKEN}\n", encoding="ascii")
        target.chmod(0o600)
        gate.symlink_to(target)
        assert client.get("/business", headers={HEADER: TOKEN}).status_code == 503


@pytest.mark.unit
def test_ingress_gate_command_is_idempotent_silent_and_rejects_symlink_targets(
    tmp_path: Path,
) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts/echodesk-ingress-gate.py"
    root = tmp_path / "gate-root"
    root.mkdir(mode=0o700)
    root = root.resolve(strict=True)
    gate = root / "deployment-gate.token"
    env = {**os.environ, "ECHODESK_DEPLOYMENT_GATE_FILE": str(gate)}
    base = [str(script)]
    suffix = ["echodesk-demo-backend.service", "8769"]

    status = subprocess.run(
        [*base, "status", *suffix], env=env, check=True, capture_output=True, text=True
    )
    assert status.stdout == "open\n"
    for _ in range(2):
        closed = subprocess.run(
            [*base, "close", *suffix], env=env, check=True, capture_output=True, text=True
        )
        assert closed.stdout == ""
    assert stat_mode(gate) == 0o600
    assert TOKEN not in gate.read_text(encoding="ascii")
    assert (
        subprocess.run(
            [*base, "status", *suffix],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "closed\n"
    )
    subprocess.run([*base, "open", *suffix], env=env, check=True, capture_output=True)
    assert not gate.exists()

    victim = root / "victim"
    victim.write_text("preserve", encoding="utf-8")
    gate.symlink_to(victim)
    rejected = subprocess.run(
        [*base, "close", *suffix],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert victim.read_text(encoding="utf-8") == "preserve"


@pytest.mark.unit
def test_public_isolation_smoke_reads_only_a_safe_gate_file(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts/public-isolation-smoke.py"
    read_token = cast(Any, runpy.run_path(str(script))["_read_deployment_gate_token"])
    gate = _gate_file(tmp_path)
    assert read_token(str(gate)) == TOKEN

    gate.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        read_token(str(gate))
    gate.unlink()
    target = _gate_file(tmp_path)
    link = tmp_path / "gate-link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        read_token(str(link))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
