"""pytest 全局配置。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 让 `pytest` 从 backend/ 跑时，import app.* 直接可用
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Test modules import ``app.main`` during collection, before any fixture can run.
# Isolate every import-time log/config/database path up front so collection can
# never touch the developer's real ~/.echodesk or backend/.env.
_COLLECTION_SANDBOX = tempfile.TemporaryDirectory(prefix="echodesk-pytest-")
_COLLECTION_ROOT = Path(_COLLECTION_SANDBOX.name)
os.environ["ECHO_USER_DIR"] = str(_COLLECTION_ROOT)
os.environ["DB_PATH"] = str(_COLLECTION_ROOT / "echodesk.db")
os.environ["STORAGE_DIR"] = str(_COLLECTION_ROOT / "storage")
os.environ["RAG_INDEX_DIR"] = str(_COLLECTION_ROOT / "rag_index")
os.environ["WORKSPACE_STATE_FILE"] = str(_COLLECTION_ROOT / "workspace_state.json")
os.environ["SKILL_EXECUTOR_BUILD_DIR"] = str(_COLLECTION_ROOT / "skill_build")

from app.config import Settings, get_settings  # noqa: E402

Settings.model_config["env_file"] = ()
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolate_non_integration_test_state(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """非真实集成测试不得读取或写入 ~/.echodesk 与本机 backend/.env。"""
    if request.node.get_closest_marker("live") is not None:
        yield
        return

    test_home = tmp_path / "echodesk-user"
    monkeypatch.setenv("ECHO_USER_DIR", str(test_home))
    monkeypatch.setenv("DB_PATH", str(test_home / "echodesk.db"))
    monkeypatch.setenv("STORAGE_DIR", str(test_home / "storage"))
    monkeypatch.setenv("RAG_INDEX_DIR", str(test_home / "rag_index"))
    monkeypatch.setenv("WORKSPACE_STATE_FILE", str(test_home / "workspace_state.json"))
    monkeypatch.setenv("SKILL_EXECUTOR_BUILD_DIR", str(test_home / "skill_build"))

    monkeypatch.setitem(Settings.model_config, "env_file", ())
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
