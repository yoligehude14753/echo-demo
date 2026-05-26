"""BM25Rag adapter 单测：tokenize / chunk / 持久化 / query。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag, RagError
from app.adapters.rag.bm25 import _tokenize_cn_en
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(rag_index_dir=tmp_path)


@pytest.mark.unit
def test_tokenize_handles_chinese_and_english() -> None:
    tokens = _tokenize_cn_en("ChatGPT 是 OpenAI 的产品")
    joined = " ".join(tokens)
    assert "chatgpt" in joined
    assert "openai" in joined
    assert "产品" in joined or "产" in joined


@pytest.mark.unit
def test_tokenize_handles_numbers_and_units() -> None:
    tokens = _tokenize_cn_en("Nvidia 8x H100 集群")
    assert "8x" in tokens or "8" in tokens
    assert "h100" in tokens or "nvidia" in tokens or "h" in tokens


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_meeting_and_query(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    transcript = (
        "今天讨论了 Echo 项目的 demo 计划。会议纪要功能已经接通。"
        "下一步计划接入跨会议 RAG 检索。Nvidia H100 集群在 heyi-bj。"
    )
    doc_id = await rag.ingest_meeting("m001", transcript, "demo 计划会")
    assert doc_id.startswith("meeting-")

    hits = await rag.query("Nvidia H100", top_k=3)
    assert hits, "should find at least one hit"
    assert any("H100" in h.text or "h100" in h.text.lower() for h in hits)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_empty_index_returns_empty(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    assert await rag.query("anything") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_removes_chunks(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    doc_id = await rag.ingest_meeting("m001", "测试内容。MeetMe 是一个产品。", "t")
    assert rag.stats()["n_chunks"] >= 1
    await rag.delete(doc_id)
    assert rag.stats()["n_chunks"] == 0
    assert await rag.query("MeetMe") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reingest_meeting_replaces_old_chunks(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    await rag.ingest_meeting("m001", "version A content", "t")
    await rag.ingest_meeting("m001", "version B different stuff", "t")
    # 只应保留 version B 的内容
    hits_a = await rag.query("version A content")
    hits_b = await rag.query("version B different stuff")
    assert not hits_a or all("version A" not in h.text for h in hits_a)
    assert hits_b


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reload_from_disk_preserves_index(tmp_path: Path) -> None:
    rag1 = BM25Rag(_settings(tmp_path))
    await rag1.ingest_meeting("m001", "持久化测试 content X", "t")
    assert rag1.stats()["n_chunks"] >= 1

    # 新实例读同目录
    rag2 = BM25Rag(_settings(tmp_path))
    hits = await rag2.query("持久化")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_nonexistent_pdf_raises(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    with pytest.raises(RagError):
        await rag.ingest_pdf("/nonexistent/path.pdf")
