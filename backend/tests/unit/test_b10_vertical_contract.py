"""B10 C-owned production vertical gate.

This test intentionally has no fake worker or fake command handler.  It is a
fail-closed evidence gate until the real B04K handler is exported and wired
through the Electron framed port into AgentTaskService.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _production_sources() -> list[Path]:
    roots = (REPO_ROOT / "backend" / "app", REPO_ROOT / "desktop" / "electron")
    files: list[Path] = []
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "node_modules" not in path.parts
            and ".venv" not in path.parts
            and "/test" not in str(path)
            and "/tests" not in str(path)
        )
    return files


@pytest.mark.unit
def test_b10_vertical_requires_real_b04k_handler_export() -> None:
    """Block acceptance until handler → framed port → service is real."""

    framed_server_source = (
        REPO_ROOT / "desktop/electron/agent-runtime/bridge/embedded-runtime-server.ts"
    ).read_text()
    production_sources = _production_sources()
    production_texts = [path.read_text(errors="replace") for path in production_sources]
    production_text = "\n".join(production_texts)

    missing: list[str] = []
    if "export class EmbeddedRuntimePortServer" not in framed_server_source:
        missing.append("Electron EmbeddedRuntimePortServer export")
    if "new EmbeddedRuntimePortServer(" not in production_text:
        missing.append("production EmbeddedRuntimePortServer instantiation")
    if not any(
        re.search(r"export\s+(?:async\s+)?function\s+createWorkerRuntime", text)
        for text in production_texts
    ):
        missing.append("production createWorkerRuntime/factoryModule binding")
    if not any(
        "createKernelWorkerRuntime(" in text
        and "OpenSessionInput" in text
        and "KernelDeps" in text
        and source.name != "bridge.ts"
        for source, text in zip(production_sources, production_texts, strict=True)
    ):
        missing.append("production OpenSessionInput/KernelDeps injection")

    if missing:
        pytest.fail("BLOCKED_HANDLER_CONTRACT: " + "; ".join(missing))
