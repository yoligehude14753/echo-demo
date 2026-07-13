from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from app.api.ws import router as ws_router
from app.main import create_app
from app.schemas.events import EchoEventType

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.arch
def test_workflow_rest_route_snapshot() -> None:
    app = create_app()
    # FastAPI >= 0.139 keeps included routers as lazy `_IncludedRouter`
    # entries, so `app.routes` is no longer a flat public route inventory.
    # OpenAPI is the stable, fully expanded HTTP contract clients consume.
    openapi_paths = app.openapi()["paths"]
    all_routes = {
        (method.upper(), path)
        for path, operations in openapi_paths.items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS", "PARAMETERS"}
    }
    routes = {
        item
        for item in all_routes
        if item[1].startswith(("/workflows", "/artifacts", "/agents"))
        or item[1]
        in {
            "/meetings/{meeting_id}/artifacts",
            "/meetings/{meeting_id}/outputs",
        }
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
        ("GET", "/agents/tasks/{task_id}/artifacts/{relpath}"),
        ("POST", "/agents/tasks/{task_id}/cancel"),
        ("POST", "/agents/tasks/{task_id}/retry"),
        ("GET", "/agents/grants"),
        ("POST", "/agents/grants/claude_code"),
        ("DELETE", "/agents/grants/{grant_id}"),
    }
    assert routes == expected


@pytest.mark.arch
def test_ws_event_type_snapshot_and_single_main_ws() -> None:
    app = create_app()
    ws_paths = [
        route.path for route in ws_router.routes if "websocket" in route.__class__.__name__.lower()
    ]
    assert ws_paths == ["/ws/echo"]
    assert str(app.url_path_for("ws_echo")) == "/ws/echo"

    event_types = set(get_args(EchoEventType))
    assert event_types == {
        "meeting.started",
        "meeting.auto_detected",
        "meeting.auto_ended",
        "meeting.state_changed",
        "meeting.segment",
        "meeting.ended",
        "meeting.todo.completed",
        "workflow.event",
        "workflow.snapshot",
        "minutes.ready",
        "minutes.failed",
        "artifact.generating",
        "artifact.ready",
        "artifact.failed",
        "agent.task.event",
        "meeting.todo.updated",
        "rag.query",
        "rag.answer.delta",
        "rag.answer.done",
        "chat.delta",
        "tts.suggested",
        "chat.done",
        "error",
        "server_hello",
        "server_ping",
        "server_resync",
        "server_sync",
        "client_hello",
        "client_ping",
    }


@pytest.mark.arch
def test_electron_ipc_channel_snapshot() -> None:
    preload = (REPO_ROOT / "desktop/electron/preload.cjs").read_text(encoding="utf-8")
    main = (REPO_ROOT / "desktop/electron/main.cjs").read_text(encoding="utf-8")
    preload_channels = set(re.findall(r'ipcRenderer\.(?:invoke|on|sendSync)\("([^"]+)"', preload))
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
def test_workflow_recovery_reaper_is_bound_to_application_lifespan() -> None:
    main_source = (REPO_ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    kernel_source = (REPO_ROOT / "backend/app/workflows/kernel.py").read_text(encoding="utf-8")

    assert "dispatcher.start_recovery_reaper(" in main_source
    assert "await aclose_workflow_service()" in main_source
    assert main_source.index("await aclose_workflow_service()") < main_source.index(
        "await aclose_llm_singleton()"
    )
    assert "async def recover_unfinished_scopes(" in kernel_source
    assert "reaper_task.cancel()" in kernel_source


@pytest.mark.arch
def test_agentos_runtime_install_contract(tmp_path: Path) -> None:
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
    assert 'config.get("llm_main_api_key", "")' in install_agentos_text
    assert "private_upstream_without_key" in install_agentos_text
    assert 'config.get("llm_main_provider") == "yunwu"' not in install_agentos_text
    assert 'config.get("llm_main_model") == "deepseek-v4-flash"' not in install_agentos_text
    assert 'AGENTOS_VENV="$DEST_ROOT/.venv"' in install_agentos_text
    assert "python-multipart" in install_agentos_text
    assert 'MAIN_PROVIDER="$(read_config llm_main_provider openai-compatible)"' in run_agentos_text
    assert "MAIN_MODEL=\"$(read_config llm_main_model '')\"" in run_agentos_text
    assert "MAIN_API_KEY=\"$(read_config llm_main_api_key '')\"" in run_agentos_text
    assert "MAIN_API_KEY=\"$(read_config yunwu_open_key '')\"" in run_agentos_text
    assert 'MAIN_API_KEY="agentos-internal-vllm-no-auth"' in run_agentos_text
    assert "public main-model API key is empty" in run_agentos_text
    assert 'AGENTOS_PROXY_UPSTREAM_API_KEY="$MAIN_API_KEY"' in run_agentos_text
    assert 'AGENTOS_DATA_DIR="${ECHODESK_AGENTOS_DATA_DIR:-$ECHODESK_HOME/agentos}"' in (
        run_agentos_text
    )
    assert "$ECHODESK_HOME/source/agentos/.venv/bin/python" in run_agentos_text
    assert "$HOME/.agentos" not in run_agentos_text

    start_marker = 'RUNNER_ENABLED="$($BACKEND_PY - "$CONFIG_PATH" <<\'PY\'\n'
    end_marker = '\nPY\n)"'
    start = install_agentos_text.index(start_marker) + len(start_marker)
    end = install_agentos_text.index(end_marker, start)
    enable_program = install_agentos_text[start:end]

    cases = (
        (
            {
                "llm_main_provider": "openai-compatible",
                "llm_main_model": "glm-5.2-int4",
                "llm_main_base_url": "http://192.168.199.179:8000/v1",
            },
            True,
        ),
        (
            {
                "llm_main_provider": "openai-compatible",
                "llm_main_model": "gpt-5.6",
                "llm_main_base_url": "https://api.example.test/v1",
            },
            False,
        ),
        (
            {
                "llm_main_provider": "openai-compatible",
                "llm_main_model": "glm-5.2-int4",
                "llm_main_base_url": "http://192.168.199.179:8000/v1?token=unsafe",
            },
            False,
        ),
        (
            {
                "llm_main_provider": "openai-compatible",
                "llm_main_model": "gpt-5.6",
                "llm_main_base_url": "https://api.example.test/v1",
                "llm_main_api_key": "test-only-key",
            },
            True,
        ),
    )
    for index, (config, expected) in enumerate(cases):
        config_path = tmp_path / f"agentos-config-{index}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c", enable_program, str(config_path)],
            check=True,
            text=True,
            capture_output=True,
        )
        assert completed.stdout.strip() == ("1" if expected else "0")
        updated = json.loads(config_path.read_text(encoding="utf-8"))
        assert updated["agent_os_enabled"] is expected
        assert updated["agent_os_url"] == "http://127.0.0.1:4128"

    private_start_marker = 'PRIVATE_UPSTREAM="$($PYTHON_BIN - "$MAIN_BASE_URL" <<\'PY\'\n'
    private_end_marker = '\nPY\n)"'
    private_start = run_agentos_text.index(private_start_marker) + len(private_start_marker)
    private_end = run_agentos_text.index(private_end_marker, private_start)
    private_program = run_agentos_text[private_start:private_end]
    for base_url, expected in (
        ("http://192.168.199.179:8000/v1", "1"),
        ("http://127.0.0.1:8000/v1", "1"),
        ("http://192.168.199.179:8000/v1?token=unsafe", "0"),
        ("https://api.example.test/v1", "0"),
        ("file:///tmp/model", "0"),
    ):
        completed = subprocess.run(
            [sys.executable, "-c", private_program, base_url],
            check=True,
            text=True,
            capture_output=True,
        )
        assert completed.stdout.strip() == expected
