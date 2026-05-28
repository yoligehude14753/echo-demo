"""P4-M3：/artifacts/{id}/download filename 用 meta.json.title 生成。

覆盖：
- meta.json 存在 → filename = <safe_title>_<artifact_id>.<ext>
- meta.json 缺失 → 回退到 output.<ext>
- title 含非法字符（< > : " / \\ | ? *）→ 全替换为空格
- title 全空白 → fallback "untitled"
- meta.json 损坏 JSON → 回退到 output.<ext>
"""

from __future__ import annotations

import email.utils
import json
from pathlib import Path

import pytest
from app.api.artifacts import _safe_title
from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path,
        skill_executor_build_dir=tmp_path / "skill_build",
    )


def _client_with_settings(tmp_path: Path) -> TestClient:
    settings = _settings(tmp_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _seed_artifact(
    tmp_path: Path,
    artifact_id: str,
    *,
    ext: str,
    body: bytes,
    meta: dict[str, str] | None,
) -> Path:
    build_dir = tmp_path / "skill_build" / artifact_id
    build_dir.mkdir(parents=True)
    out = build_dir / f"output.{ext}"
    out.write_bytes(body)
    if meta is not None:
        (build_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return build_dir


def _parse_content_disposition_filename(header: str) -> str:
    """从 Content-Disposition 头解析 filename（支持 RFC2231 filename*= UTF-8 编码）。"""
    msg = email.message.EmailMessage()
    msg["content-disposition"] = header
    params = msg["content-disposition"].params
    # FastAPI/Starlette 对非 ASCII filename 用 filename*= UTF-8''<encoded>
    if "filename*" in params:
        # email.headerregistry already decodes filename*= for us when accessed
        return str(params["filename*"])
    return str(params.get("filename", ""))


@pytest.mark.unit
def test_safe_title_replaces_unsafe_chars() -> None:
    # 不允许字符替换为空格，连续多空白折叠成单空格
    assert _safe_title('hello: "world"/<x>|?') == "hello world x"


@pytest.mark.unit
def test_safe_title_fallback_for_empty() -> None:
    assert _safe_title("") == "untitled"
    assert _safe_title("   \n\t  ") == "untitled"
    assert _safe_title("///") == "untitled"


@pytest.mark.unit
def test_safe_title_truncates_long_input() -> None:
    long = "x" * 500
    assert len(_safe_title(long)) <= 120


@pytest.mark.unit
def test_download_uses_meta_title_for_filename(tmp_path: Path) -> None:
    client = _client_with_settings(tmp_path)
    _seed_artifact(
        tmp_path,
        "html-abc1234567",
        ext="html",
        body=b"<html>hi</html>",
        meta={"title": "英伟达 2025 Q3 财报点评", "artifact_type": "html", "ext": "html"},
    )
    r = client.get("/artifacts/html-abc1234567/download")
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    fname = _parse_content_disposition_filename(cd)
    assert "html-abc1234567" in fname
    assert fname.endswith(".html")
    # title 应进入 filename
    assert "英伟达" in fname or "Q3" in fname
    assert "财报点评" in fname or "财报" in fname


@pytest.mark.unit
def test_download_sanitizes_unsafe_title(tmp_path: Path) -> None:
    client = _client_with_settings(tmp_path)
    _seed_artifact(
        tmp_path,
        "md-bad0000000",
        ext="md",
        body=b"# hi",
        meta={"title": 'evil:"<title>"/?', "artifact_type": "markdown", "ext": "md"},
    )
    r = client.get("/artifacts/md-bad0000000/download")
    assert r.status_code == 200
    fname = _parse_content_disposition_filename(r.headers["content-disposition"])
    # 非法字符全部移除
    for ch in '<>:"/\\|?*':
        assert ch not in fname
    assert fname.endswith(".md")
    assert "md-bad0000000" in fname


@pytest.mark.unit
def test_download_meta_missing_falls_back_to_output_name(tmp_path: Path) -> None:
    """meta.json 不存在（旧 artifact 兼容） → filename 回退到 output.<ext>。"""
    client = _client_with_settings(tmp_path)
    _seed_artifact(
        tmp_path,
        "old-no-meta-0",
        ext="docx",
        body=b"PK\x03\x04",  # zip magic, 内容 stub
        meta=None,
    )
    r = client.get("/artifacts/old-no-meta-0/download")
    assert r.status_code == 200
    fname = _parse_content_disposition_filename(r.headers["content-disposition"])
    assert fname == "output.docx"


@pytest.mark.unit
def test_download_corrupted_meta_falls_back(tmp_path: Path) -> None:
    """meta.json 是非法 JSON → 不抛 500，回退到 output.<ext>。"""
    client = _client_with_settings(tmp_path)
    build_dir = _seed_artifact(
        tmp_path,
        "corrupt-1",
        ext="txt",
        body=b"hello",
        meta=None,
    )
    (build_dir / "meta.json").write_text("{ not valid json", encoding="utf-8")
    r = client.get("/artifacts/corrupt-1/download")
    assert r.status_code == 200
    fname = _parse_content_disposition_filename(r.headers["content-disposition"])
    assert fname == "output.txt"


@pytest.mark.unit
def test_download_empty_title_uses_untitled(tmp_path: Path) -> None:
    client = _client_with_settings(tmp_path)
    _seed_artifact(
        tmp_path,
        "empty-title-1",
        ext="pdf",
        body=b"%PDF-1.4 stub",
        meta={"title": "   ", "artifact_type": "pdf", "ext": "pdf"},
    )
    r = client.get("/artifacts/empty-title-1/download")
    assert r.status_code == 200
    fname = _parse_content_disposition_filename(r.headers["content-disposition"])
    assert "untitled" in fname
    assert fname.endswith(".pdf")
    assert "empty-title-1" in fname


@pytest.mark.unit
def test_download_artifact_not_found(tmp_path: Path) -> None:
    client = _client_with_settings(tmp_path)
    r = client.get("/artifacts/does-not-exist/download")
    assert r.status_code == 404


@pytest.mark.unit
def test_download_output_missing(tmp_path: Path) -> None:
    """artifact 目录存在但没有 output.* 文件 → 404 'output file missing'。"""
    client = _client_with_settings(tmp_path)
    (tmp_path / "skill_build" / "empty-dir").mkdir(parents=True)
    r = client.get("/artifacts/empty-dir/download")
    assert r.status_code == 404
