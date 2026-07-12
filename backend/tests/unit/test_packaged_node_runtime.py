from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.adapters.skill.llm_skill import run_packaged_ppt_runtime_smoke
from app.adapters.skill.node_executor import node_runtime_environment, run_node_script
from app.config import Settings


@pytest.mark.unit
def test_settings_resolve_electron_node_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECHODESK_NODE_RUNTIME", "/opt/EchoDesk/EchoDesk")
    monkeypatch.setenv("ECHODESK_NODE_RUNTIME_IS_ELECTRON", "1")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.resolved_skill_node_bin == "/opt/EchoDesk/EchoDesk"
    assert settings.resolved_skill_node_is_electron is True


@pytest.mark.unit
def test_explicit_skill_node_runtime_is_not_masked_by_electron_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHODESK_NODE_RUNTIME", "/opt/EchoDesk/EchoDesk")
    monkeypatch.setenv("ECHODESK_NODE_RUNTIME_IS_ELECTRON", "1")

    settings = Settings(skill_node_bin="/admin/selected/node", _env_file=None)  # type: ignore[call-arg]

    assert settings.resolved_skill_node_bin == "/admin/selected/node"
    assert settings.resolved_skill_node_is_electron is False


@pytest.mark.unit
def test_electron_node_environment_is_explicit_and_does_not_forward_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHODESK_TEST_SECRET", "must-not-leak")

    env = node_runtime_environment(tmp_path, electron_runtime=True)

    assert env["ELECTRON_RUN_AS_NODE"] == "1"
    assert env["NODE_PATH"] == str((tmp_path / "node_modules").resolve())
    assert "ECHODESK_TEST_SECRET" not in env


@pytest.mark.unit
def test_run_node_script_uses_electron_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    monkeypatch.setattr("app.adapters.skill.node_executor.subprocess.run", fake_run)
    script = tmp_path / "render.mjs"
    rc, output = run_node_script(
        node_bin="/Applications/EchoDesk.app/Contents/MacOS/EchoDesk",
        script_path=script,
        args=["input.json", "output.pptx"],
        cwd=tmp_path,
        node_modules_root=tmp_path,
        electron_runtime=True,
        timeout_s=10,
    )

    assert rc == 0
    assert output == "ok"
    assert captured["command"] == [
        "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk",
        str(script),
        "input.json",
        "output.pptx",
    ]
    assert captured["env"]["ELECTRON_RUN_AS_NODE"] == "1"  # type: ignore[index]


@pytest.mark.unit
def test_fixed_packaged_ppt_assets_render_without_npm(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "deterministic test gate must provide Node"

    output = run_packaged_ppt_runtime_smoke(
        tmp_path,
        node_bin=node,
        electron_runtime=False,
    )

    assert output.name == "runtime-smoke.pptx"
    assert output.stat().st_size > 8_000
