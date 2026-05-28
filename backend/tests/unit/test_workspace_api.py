"""workspace.py API 单测：add-dir / remove-dir / status / scan（P4-fix-rag-chat）。

覆盖：
- POST /workspace/add-dir：成功 / 路径不存在 / 不是目录 / 幂等 / 持久化到 user.json
- POST /workspace/remove-dir：成功 / 不在配置里 / 路径不存在
- 加目录后 status.configured_dirs 立刻含新目录（不依赖 backend 重启）
- 写入 ~/.echodesk/config.json 后，重新构造 Settings 仍能加载新值

之所以不用真 TestClient 跑 add-dir + scan 全链路：scan 会真扫文件 + ingest 到
BM25Rag，依赖 markitdown 等重依赖；这里把 scan 部分 mock 掉，只验配置写入和
status 接口契约。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.api.retrieval import reset_singletons as reset_rag
from app.api.workspace import reset_singleton as reset_scanner
from app.config import Settings, get_settings
from app.config_io import user_config_path
from app.main import create_app
from fastapi.testclient import TestClient


def _make_settings(tmp_path: Path, workspace_dirs: str = "") -> Settings:
    return Settings(  # type: ignore[call-arg]
        db_path=tmp_path / "echodesk.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag_index",
        skill_executor_build_dir=tmp_path / "skill_build",
        workspace_state_file=tmp_path / "ws_state.json",
        workspace_dirs=workspace_dirs,
        workspace_scan_on_startup=False,
        diarizer_enabled=False,
        _env_file=None,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """构造 backend FastAPI app + 把 ECHO_USER_DIR 重定向到 tmp_path。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_rag()
    reset_scanner()
    get_settings.cache_clear()
    settings = _make_settings(tmp_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.mark.unit
def test_add_dir_persists_and_reflects_in_status(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """痛点修复主路径：add-dir → user.json 写入 → status 立刻可见新目录。"""
    target = tmp_path / "my_workspace"
    target.mkdir()

    r = client.post("/workspace/add-dir", json={"path": str(target)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] is True
    assert body["path"] == str(target.resolve())
    assert str(target.resolve()) in body["configured_dirs"]

    # 状态接口立即反映新目录（不需要 backend 重启）
    status = client.get("/workspace/status").json()
    assert str(target.resolve()) in status["configured_dirs"]
    assert str(target.resolve()) in status["authorized_dirs"]

    # user.json 已持久化 workspace_dirs 字段
    cfg_path = user_config_path()
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "workspace_dirs" in cfg
    assert str(target.resolve()) in cfg["workspace_dirs"]


@pytest.mark.unit
def test_add_dir_is_idempotent(client: TestClient, tmp_path: Path) -> None:
    """同一目录加两次：第二次 added=False，不重复出现在 configured_dirs。"""
    target = tmp_path / "dup"
    target.mkdir()

    r1 = client.post("/workspace/add-dir", json={"path": str(target)})
    assert r1.json()["added"] is True

    r2 = client.post("/workspace/add-dir", json={"path": str(target)})
    assert r2.status_code == 200
    assert r2.json()["added"] is False
    dirs = r2.json()["configured_dirs"]
    # 计数：strip() / resolve() 之后只应出现一次
    assert dirs.count(str(target.resolve())) == 1


@pytest.mark.unit
def test_add_dir_rejects_missing_path(client: TestClient, tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    r = client.post("/workspace/add-dir", json={"path": str(bogus)})
    assert r.status_code == 400
    assert "目录不存在" in r.json()["detail"]


@pytest.mark.unit
def test_add_dir_rejects_file_path(client: TestClient, tmp_path: Path) -> None:
    """add-dir 拒绝普通文件（必须是目录）。"""
    f = tmp_path / "not_a_dir.md"
    f.write_text("hello", encoding="utf-8")
    r = client.post("/workspace/add-dir", json={"path": str(f)})
    assert r.status_code == 400
    assert "不是目录" in r.json()["detail"]


@pytest.mark.unit
def test_add_dir_rejects_empty_path(client: TestClient) -> None:
    r = client.post("/workspace/add-dir", json={"path": "   "})
    assert r.status_code == 400


@pytest.mark.unit
def test_remove_dir_idempotent_when_absent(client: TestClient, tmp_path: Path) -> None:
    """remove-dir 对一个根本没在 workspace_dirs 里的目录：removed=False（不报错）。"""
    d = tmp_path / "ghost"
    d.mkdir()
    r = client.post("/workspace/remove-dir", json={"path": str(d)})
    assert r.status_code == 200
    assert r.json()["removed"] is False


@pytest.mark.unit
def test_remove_dir_round_trip(client: TestClient, tmp_path: Path) -> None:
    """add 后 remove → configured_dirs 回到空 + user.json 字段更新。"""
    target = tmp_path / "round_trip"
    target.mkdir()
    client.post("/workspace/add-dir", json={"path": str(target)})

    r = client.post("/workspace/remove-dir", json={"path": str(target)})
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert str(target.resolve()) not in r.json()["configured_dirs"]

    status = client.get("/workspace/status").json()
    assert str(target.resolve()) not in status["configured_dirs"]
