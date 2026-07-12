"""Integration: deterministic PDF ingest + opt-in live Tavily contract."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.web_search import TavilyWebSearch
from app.config import Settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_pdf_ingest_and_query(tmp_path: Path) -> None:
    from fpdf import FPDF

    fixture = tmp_path / "enterprise-ai-fixture.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0,
        8,
        "Enterprise AI adoption report. ChatGPT usage and enterprise adoption "
        "increased across finance, support, engineering, and operations.",
    )
    pdf.output(str(fixture))
    s = Settings(rag_index_dir=tmp_path, rag_pdf_chunk_tokens=600, rag_pdf_chunk_overlap=100)
    rag = BM25Rag(s)
    doc_id = await rag.ingest_pdf(str(fixture), doc_title="Enterprise AI fixture")
    assert doc_id.startswith("pdf-")

    stats = rag.stats()
    assert stats["n_chunks"] >= 1

    # 该报告里的关键术语；不强求 top1，但 top5 应该至少 1 个 chunk 文本里包含
    hits = await rag.query("ChatGPT enterprise adoption", top_k=5)
    assert hits, "top-5 should not be empty"
    bag = " ".join(h.text.lower() for h in hits)
    assert any(k in bag for k in ("chatgpt", "enterprise", "adoption", "ai"))


def _has_tavily_key() -> bool:
    return bool(os.getenv("TAVILY_API_KEY") or Settings().tavily_api_key)


@pytest.mark.asyncio
@pytest.mark.live
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
