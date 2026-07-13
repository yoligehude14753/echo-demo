from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.arch
def test_desktop_and_backend_share_canonical_local_endpoint() -> None:
    config = json.loads((ROOT / "desktop" / "backend.config.json").read_text(encoding="utf-8"))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert config == {
        "local": {"host": "127.0.0.1", "port": 8769},
        "lanShare": {"enabled": True, "bindHost": "0.0.0.0"},
        "public": {"baseUrl": "https://echodesk.yoliyoli.uk"},
    }
    port = config["local"]["port"]
    assert settings.port == port
    assert settings.public_http_url == f"http://localhost:{port}"
    assert settings.public_ws_url == f"ws://localhost:{port}/ws/echo"


@pytest.mark.arch
def test_desktop_runtime_has_no_legacy_8772_endpoint() -> None:
    paths = [
        ROOT / "desktop" / "electron" / "main.cjs",
        ROOT / "desktop" / "electron" / "backend-endpoint.cjs",
        ROOT / "desktop" / "electron" / "preload.cjs",
        ROOT / "desktop" / "src" / "runtime.ts",
        ROOT / "desktop" / "src" / "api.ts",
        ROOT / "desktop" / "vite.config.ts",
    ]
    offenders = [str(path.relative_to(ROOT)) for path in paths if "8772" in path.read_text()]
    assert offenders == []


@pytest.mark.arch
def test_desktop_endpoint_consumers_do_not_use_legacy_flat_config_keys() -> None:
    paths = [
        ROOT / "desktop" / "electron" / "main.cjs",
        ROOT / "desktop" / "src" / "runtime.ts",
        ROOT / "desktop" / "vite.config.ts",
    ]
    legacy_expressions = (
        "backendConfig.localHost",
        "backendConfig.bindHost",
        "backendConfig.port",
        "backendConfig.publicBase",
    )
    offenders = [
        f"{path.relative_to(ROOT)}:{expression}"
        for path in paths
        for expression in legacy_expressions
        if expression in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.arch
def test_every_electron_packaging_path_requires_the_bundled_backend() -> None:
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    windows_release = (ROOT / "desktop" / "scripts" / "desktop-release-signing.cjs").read_text(
        encoding="utf-8"
    )
    windows_release = windows_release.split("async function runWindowsRelease", maxsplit=1)[1]
    assert package["build"]["beforePack"] == "scripts/verify-bundled-backend.cjs"
    assert package["scripts"]["app:dist:win"] == "node scripts/desktop-release-signing.cjs windows"
    assert windows_release.index('"backend:build:win"') < windows_release.index(
        '"electron-builder"'
    )
    assert (ROOT / "backend" / "packaging" / "echodesk-backend.spec").is_file()


@pytest.mark.arch
def test_controlled_uvicorn_launchers_cap_websocket_messages_at_protocol_limit() -> None:
    entrypoint = (ROOT / "backend" / "packaging" / "entrypoint.py").read_text(encoding="utf-8")
    electron = (ROOT / "desktop" / "electron" / "main.cjs").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "public-backend-deploy.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-backend.sh").read_text(encoding="utf-8")

    assert "choices=(4096,)" in entrypoint
    assert "ws_max_size=server_args.ws_max_size" in entrypoint
    assert electron.count('"--ws-max-size"') == 2
    assert electron.count('"4096"') >= 2
    assert "--ws-max-size 4096" in deploy
    assert "--ws-max-size 4096" in installer


@pytest.mark.arch
def test_documented_uvicorn_commands_preserve_protocol_frame_cap() -> None:
    paths = (
        ROOT / "README.md",
        ROOT / "backend" / "README.md",
        ROOT / "backend" / "app" / "main.py",
        ROOT / "scripts" / "demo_run.py",
        ROOT / "docs" / "TV_INSTALL.md",
        ROOT / "docs" / "DEMO_GUIDE.md",
        ROOT / "docs" / "INSTALL.md",
        ROOT / "docs" / "SCENARIO_VERIFICATION.md",
        ROOT / "desktop" / "playwright.real.config.ts",
        ROOT / "desktop" / "playwright.demo.config.ts",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert source.count("uvicorn app.main:app") == source.count("--ws-max-size 4096"), path


@pytest.mark.arch
def test_artifact_urls_use_the_electron_authoritative_backend_snapshot() -> None:
    main_source = (ROOT / "desktop" / "electron" / "main.cjs").read_text(encoding="utf-8")
    preload_source = (ROOT / "desktop" / "electron" / "preload.cjs").read_text(encoding="utf-8")
    runtime_source = (ROOT / "desktop" / "src" / "runtime.ts").read_text(encoding="utf-8")
    api_source = (ROOT / "desktop" / "src" / "api.ts").read_text(encoding="utf-8")

    assert 'ipcMain.on("echo:backend-host-sync"' in main_source
    assert 'backendHost: ipcRenderer.sendSync("echo:backend-host-sync")' in preload_source
    assert "window.echo?.backendHost" in runtime_source
    artifact_function = api_source.split("export function artifactDownloadUrl", maxsplit=1)[1]
    artifact_function = artifact_function.split("\n}\n", maxsplit=1)[0]
    assert "backendBaseSnapshot()" in artifact_function
    assert "8772" not in artifact_function
