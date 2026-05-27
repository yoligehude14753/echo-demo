"""Demo server 集成测试：用 FastAPI TestClient 跑 HTTP 流程。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Demo server 在 demo/server.py 而非包内 —— 用 path hack import 它
_DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"


@pytest.fixture
def demo_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """启动 demo app，patch OpenAIClient → MockLLMClient。"""
    sys.path.insert(0, str(_DEMO_DIR))
    try:
        # 改 OUT_BASE 到 tmp_path 避免污染真实目录
        import importlib

        import server as demo_server  # type: ignore[import-not-found]
        importlib.reload(demo_server)
        monkeypatch.setattr(demo_server, "OUT_BASE", tmp_path / "runs")
        (tmp_path / "runs").mkdir(parents=True, exist_ok=True)

        # 重新 mount StaticFiles 指到新目录
        from fastapi.staticfiles import StaticFiles
        # 清掉旧 mount
        demo_server.app.router.routes = [
            r for r in demo_server.app.router.routes if getattr(r, "name", None) != "runs"
        ]
        demo_server.app.mount(
            "/runs",
            StaticFiles(directory=str(tmp_path / "runs")),
            name="runs",
        )

        # Patch LLM
        from tests.conftest import MockLLMClient
        monkeypatch.setattr(demo_server, "OpenAIClient", lambda model=None: MockLLMClient())

        # 让 health endpoint 觉得已配置
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")

        yield demo_server.app
    finally:
        if str(_DEMO_DIR) in sys.path:
            sys.path.remove(str(_DEMO_DIR))


def test_health_endpoint(demo_app):
    from fastapi.testclient import TestClient
    client = TestClient(demo_app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_index_serves_html(demo_app):
    from fastapi.testclient import TestClient
    client = TestClient(demo_app)
    r = client.get("/")
    assert r.status_code == 200
    assert "minutes_kit · demo" in r.text
    assert "<textarea" in r.text


def test_list_runs_empty(demo_app):
    from fastapi.testclient import TestClient
    client = TestClient(demo_app)
    r = client.get("/api/runs")
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_generate_endpoint_e2e(demo_app, tmp_path: Path):
    from fastapi.testclient import TestClient
    client = TestClient(demo_app)

    transcript_text = (
        "[10:00:00] A: 我们对一下产物自动化\n"
        "[10:00:10] B: Word 我来\n"
        "[10:00:20] C: Excel 我来\n"
    )
    r = client.post(
        "/generate",
        data={
            "transcript_text": transcript_text,
            "title_hint": "测试",
            "participants": "A,B,C",
            "use_claude": "",  # 跳过 claude，走 fallback
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "run_id" in body
    assert body["preview_url"].endswith("/preview.html")
    assert body["docx_url"] is not None
    assert body["docx_generator"] in ("python_fallback", "claude")

    # 拉 preview.html 看是否可下载
    r2 = client.get(body["preview_url"])
    assert r2.status_code == 200
    assert "<!DOCTYPE html>" in r2.text

    # data.json 可读
    r3 = client.get(body["data_url"])
    assert r3.status_code == 200
    data = r3.json()
    assert data["title"] == "周三例会"

    # /api/runs 现在能看到这次产物
    r4 = client.get("/api/runs")
    assert r4.status_code == 200
    runs = r4.json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["run_id"] == body["run_id"]


def test_generate_endpoint_empty_transcript_400(demo_app):
    from fastapi.testclient import TestClient
    client = TestClient(demo_app)
    r = client.post("/generate", data={"transcript_text": "   \n"})
    assert r.status_code == 400
