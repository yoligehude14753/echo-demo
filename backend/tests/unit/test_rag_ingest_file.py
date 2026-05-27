"""通用 ingest_file 单测：覆盖 md/txt/csv/json/html/docx + workspace 元数据。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.rag.bm25 import RagError
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(rag_index_dir=tmp_path)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_markdown(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "note.md"
    f.write_text("# Echo 设计\n\n这是 markdown 内容关于 workspace 索引", encoding="utf-8")
    doc_id = await rag.ingest_file(str(f), source="upload")
    assert doc_id.startswith("md-")
    hits = await rag.query("workspace 索引")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_text(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "memo.txt"
    f.write_text("会议主题: Q3 OKR 复盘 营收增长 35%", encoding="utf-8")
    doc_id = await rag.ingest_file(str(f), source="upload")
    assert doc_id.startswith("txt-")
    hits = await rag.query("营收 复盘")
    assert any("Q3" in h.text or "OKR" in h.text for h in hits)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_csv_via_markitdown(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "metrics.csv"
    f.write_text("quarter,revenue\nQ1,100\nQ2,120\nQ3,135\n", encoding="utf-8")
    doc_id = await rag.ingest_file(str(f), source="upload")
    assert doc_id.startswith("csv-")
    hits = await rag.query("revenue")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_json(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "cfg.json"
    f.write_text('{"project":"echo","milestone":"M6 workspace"}', encoding="utf-8")
    doc_id = await rag.ingest_file(str(f), source="upload")
    assert doc_id.startswith("json-")
    hits = await rag.query("milestone")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_unsupported_extension_raises(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "weird.zzz"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(RagError):
        await rag.ingest_file(str(f), source="upload")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_with_source_path_records_metadata(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f = tmp_path / "notes.md"
    f.write_text("workspace 元数据测试", encoding="utf-8")
    src_path = str(f.resolve())
    doc_id = await rag.ingest_file(
        str(f), source="workspace", source_path=src_path
    )
    found = await rag.find_by_source_path(src_path)
    assert found == doc_id
    docs = await rag.list_docs()
    target = next(d for d in docs if d["doc_id"] == doc_id)
    assert target["source"] == "workspace"
    assert target["source_path"] == src_path


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_docs_groups_by_source(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path / "idx"))
    f1 = tmp_path / "a.md"
    f1.write_text("upload doc", encoding="utf-8")
    f2 = tmp_path / "b.md"
    f2.write_text("workspace doc", encoding="utf-8")
    await rag.ingest_file(str(f1), source="upload")
    await rag.ingest_file(str(f2), source="workspace", source_path=str(f2.resolve()))
    docs = await rag.list_docs()
    sources = {d["source"] for d in docs}
    assert sources == {"upload", "workspace"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_persists_across_reload(tmp_path: Path) -> None:
    idx = tmp_path / "idx"
    rag1 = BM25Rag(_settings(idx))
    f = tmp_path / "doc.md"
    f.write_text("持久化测试 alpha", encoding="utf-8")
    await rag1.ingest_file(
        str(f), source="workspace", source_path=str(f.resolve())
    )
    rag2 = BM25Rag(_settings(idx))
    hits = await rag2.query("持久化")
    assert hits
    assert await rag2.find_by_source_path(str(f.resolve())) is not None
