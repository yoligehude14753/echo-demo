"""Integration: 真实 PDF（如已下载）+ 真实 Tavily API。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.web_search import TavilyWebSearch
from app.config import Settings

pytestmark = pytest.mark.integration

ECHO_PDF = Path.home() / "Downloads" / "the-state-of-enterprise-ai_2025-report.pdf"


@pytest.mark.asyncio
@pytest.mark.skipif(not ECHO_PDF.exists(), reason=f"{ECHO_PDF.name} not in ~/Downloads")
async def test_real_pdf_ingest_and_query(tmp_path: Path) -> None:
    s = Settings(rag_index_dir=tmp_path, rag_pdf_chunk_tokens=600, rag_pdf_chunk_overlap=100)
    rag = BM25Rag(s)
    doc_id = await rag.ingest_pdf(str(ECHO_PDF), doc_title="Enterprise AI 2025")
    assert doc_id.startswith("pdf-")

    stats = rag.stats()
    assert stats["n_chunks"] > 30, f"expected ≥30 chunks for 10MB PDF, got {stats['n_chunks']}"

    # 该报告里的关键术语；不强求 top1，但 top5 应该至少 1 个 chunk 文本里包含
    hits = await rag.query("ChatGPT enterprise adoption", top_k=5)
    assert hits, "top-5 should not be empty"
    bag = " ".join(h.text.lower() for h in hits)
    assert any(k in bag for k in ("chatgpt", "enterprise", "adoption", "ai"))


def _has_tavily_key() -> bool:
    return bool(os.getenv("TAVILY_API_KEY") or Settings().tavily_api_key)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_tavily_key(), reason="TAVILY_API_KEY not set")
async def test_real_tavily_search() -> None:
    web = TavilyWebSearch(Settings())
    hits = await web.search("Nvidia H100 GPU specs", top_n=3)
    assert hits, "Tavily returned empty"
    assert all(h.source == "tavily" for h in hits)
    assert all(h.url.startswith("http") for h in hits)
    # 主要关键词应在 snippet 或 title 中出现
    bag = " ".join((h.title + " " + h.snippet).lower() for h in hits)
    assert any(k in bag for k in ("h100", "nvidia", "gpu"))
