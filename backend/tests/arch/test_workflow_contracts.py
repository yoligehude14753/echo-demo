from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import get_args

import pytest
from app.main import create_app
from app.schemas.events import EchoEventType

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.arch
def test_workflow_rest_route_snapshot() -> None:
    app = create_app()
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method not in {"HEAD", "OPTIONS"}
    }
    expected = {
        ("GET", "/workflows/runs"),
        ("GET", "/workflows/runs/{run_id}"),
        ("GET", "/workflows/runs/{run_id}/events"),
        ("POST", "/workflows/runs/{run_id}/cancel"),
        ("POST", "/workflows/runs/{run_id}/retry"),
        ("GET", "/artifacts"),
        ("POST", "/artifacts/generate"),
        ("GET", "/artifacts/{artifact_id}/download"),
        ("GET", "/meetings/{meeting_id}/artifacts"),
        ("DELETE", "/meetings/{meeting_id}/outputs"),
        ("POST", "/agents/tasks"),
        ("GET", "/agents/tasks"),
        ("GET", "/agents/tasks/{task_id}"),
        ("GET", "/agents/tasks/{task_id}/events"),
        ("POST", "/agents/tasks/{task_id}/cancel"),
        ("POST", "/agents/tasks/{task_id}/retry"),
    }
    assert expected <= routes


@pytest.mark.arch
def test_ws_event_type_snapshot_and_single_main_ws() -> None:
    app = create_app()
    ws_paths = [route.path for route in app.routes if "websocket" in route.__class__.__name__.lower()]
    assert ws_paths == ["/ws/echo"]

    event_types = set(get_args(EchoEventType))
    assert {
        "workflow.event",
        "workflow.snapshot",
        "artifact.ready",
        "artifact.failed",
        "agent.task.event",
        "meeting.todo.updated",
    } <= event_types


@pytest.mark.arch
def test_electron_ipc_channel_snapshot() -> None:
    preload = (REPO_ROOT / "desktop/electron/preload.cjs").read_text(encoding="utf-8")
    main = (REPO_ROOT / "desktop/electron/main.cjs").read_text(encoding="utf-8")
    preload_channels = set(
        re.findall(r'ipcRenderer\.(?:invoke|on|sendSync)\("([^"]+)"', preload)
    )
    main_channels = set(re.findall(r'ipcMain\.(?:handle|on)\("([^"]+)"', main))
    expected = {
        "echo:backend-host",
        "echo:share-backend-host",
        "echo:is-public-demo",
        "backend:manual-restart",
        "echo:open-artifact-in-system",
        "workspace:scan-local",
        "workspace:clear-local-docs",
        "mic:status",
        "mic:request",
    }
    assert expected <= preload_channels
    assert expected <= main_channels
    assert "backend:status" in preload_channels


@pytest.mark.arch
def test_desktop_script_matrix_snapshot() -> None:
    package_json = json.loads((REPO_ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    scripts = set(package_json.get("scripts", {}))
    assert {
        "typecheck",
        "build",
        "lint",
        "e2e",
        "e2e:real",
        "scenarios",
        "version:check",
        "app:dist:mac",
        "app:dist:win",
        "app:dist:linux",
    } <= scripts


@pytest.mark.arch
def test_agentos_runtime_install_contract() -> None:
    install_backend = REPO_ROOT / "scripts/install-backend.sh"
    install_agentos = REPO_ROOT / "scripts/install-agentos.sh"
    run_agentos = REPO_ROOT / "scripts/run-agentos.sh"
    for script in (install_backend, install_agentos, run_agentos):
        assert os.access(script, os.X_OK), f"script must be executable: {script}"
        subprocess.run(["bash", "-n", str(script)], check=True)

    install_backend_text = install_backend.read_text(encoding="utf-8")
    install_agentos_text = install_agentos.read_text(encoding="utf-8")
    run_agentos_text = run_agentos.read_text(encoding="utf-8")
    assert "step9_install_agentos" in install_backend_text
    assert 'config["agent_os_enabled"] = enabled' in install_agentos_text
    assert 'AGENTOS_VENV="$DEST_ROOT/.venv"' in install_agentos_text
    assert 'python-multipart' in install_agentos_text
    assert 'MAIN_PROVIDER="$(read_config llm_main_provider yunwu)"' in run_agentos_text
    assert 'MAIN_MODEL="$(read_config llm_main_model deepseek-v4-flash)"' in run_agentos_text
    assert 'AGENTOS_DATA_DIR="${ECHODESK_AGENTOS_DATA_DIR:-$ECHODESK_HOME/agentos}"' in (
        run_agentos_text
    )
    assert '$ECHODESK_HOME/source/agentos/.venv/bin/python' in run_agentos_text
    assert "$HOME/.agentos" not in run_agentos_text
