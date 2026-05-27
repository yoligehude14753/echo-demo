"""授权工作区扫描器单测：增量 / 删除 / 失败容错。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.rag.workspace_scanner import WorkspaceScanner
from app.config import Settings


def _make(tmp_path: Path, dirs: list[Path], **kw: object) -> tuple[BM25Rag, WorkspaceScanner]:
    idx = tmp_path / "idx"
    state = tmp_path / "ws_state.json"
    s = Settings(
        rag_index_dir=idx,
        workspace_dirs=",".join(str(d) for d in dirs),
        workspace_state_file=state,
        **kw,  # type: ignore[arg-type]
    )
    rag = BM25Rag(s)
    scanner = WorkspaceScanner(s, rag)
    return rag, scanner


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_empty_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 0
    assert r.n_added == 0
    assert (await rag.list_docs()) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_indexes_supported_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha workspace 内容", encoding="utf-8")
    (ws / "b.txt").write_text("beta plain text 内容", encoding="utf-8")
    (ws / "c.bin").write_bytes(b"\x00\x01")  # 不支持，应跳过
    sub = ws / "sub"
    sub.mkdir()
    (sub / "d.md").write_text("递归 子目录 gamma", encoding="utf-8")

    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 3
    assert r.n_added == 3
    docs = await rag.list_docs()
    assert len(docs) == 3
    assert all(d["source"] == "workspace" for d in docs)
    hits = await rag.query("子目录 gamma")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_skips_unchanged_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "stable.md"
    f.write_text("stable content alpha", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])

    r1 = await scanner.scan()
    assert r1.n_added == 1

    r2 = await scanner.scan()
    assert r2.n_added == 0
    assert r2.n_skipped == 1
    assert (await rag.list_docs())[0]["doc_id"] is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_updates_changed_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "evolving.md"
    f.write_text("first version foo", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()

    # 改文件内容（用 mtime 显式设置确保增量逻辑触发，避免 async 中 sleep）
    f.write_text("second version bar baz qux", encoding="utf-8")
    import os

    new_mtime = f.stat().st_mtime + 2.0
    os.utime(f, (new_mtime, new_mtime))

    r = await scanner.scan()
    assert r.n_updated == 1
    docs = await rag.list_docs()
    assert len(docs) == 1
    hits = await rag.query("bar baz")
    assert hits
    hits_old = await rag.query("first version foo")
    assert not any("first version foo" in h.text for h in hits_old)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_removes_deleted_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "ephemeral.md"
    f.write_text("temporary doc 123", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    assert len(await rag.list_docs()) == 1

    f.unlink()
    r = await scanner.scan()
    assert r.n_removed == 1
    assert (await rag.list_docs()) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_ignores_hidden_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("should be ignored", encoding="utf-8")
    (ws / "visible.md").write_text("visible workspace doc", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 1
    docs = await rag.list_docs()
    assert all("git" not in str(d.get("source_path", "")) or "/.git/" not in str(d.get("source_path", "")) for d in docs)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_respects_max_file_size(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    big = ws / "big.txt"
    big.write_text("x" * 200_000, encoding="utf-8")
    small = ws / "small.txt"
    small.write_text("ok small", encoding="utf-8")

    rag, scanner = _make(
        tmp_path, [ws], workspace_max_file_mb=0.1
    )  # 100 KB 上限
    r = await scanner.scan()
    assert r.n_total == 1  # 只有 small 通过
    docs = await rag.list_docs()
    assert len(docs) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_status_reports_dirs_and_count(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha", encoding="utf-8")
    _, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    st = await scanner.status()
    assert str(ws) in st["authorized_dirs"]
    assert st["n_indexed"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_only_removes_workspace_source(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ws_doc.md").write_text("workspace doc", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()

    # 另外注入一个 upload 来源的 doc
    other = tmp_path / "uploaded.md"
    other.write_text("user uploaded", encoding="utf-8")
    await rag.ingest_file(str(other), source="upload")

    assert len(await rag.list_docs()) == 2
    n = await scanner.clear()
    assert n == 1
    docs = await rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["source"] == "upload"
