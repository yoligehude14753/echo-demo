"""会议 pipeline E2E（真 Yunwu M2.7 LLM 生纪要，mock STT/Diarizer/RAG）。

只验证 finalize_meeting 这一段，因为 STT/Diarizer 已有自己的集成测试。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline

from tests.unit.test_meeting_pipeline import FakeDiarizer, FakeSTT


def _yunwu_alive() -> bool:
    if not os.getenv("YUNWU_OPEN_KEY"):
        return False
    try:
        with socket.create_connection(("yunwu.ai", 443), timeout=3):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _yunwu_alive(), reason="YUNWU_OPEN_KEY 未设置或网络不可达"),
]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_minutes_generation(tmp_path: Path) -> None:
    s = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
    )
    llm = OpenAICompatibleLLM(s)
    rag = BM25Rag(s)
    transcript = (
        "今天讨论 Q3 预算，原方案 100 万。",
        "我建议砍 30%，因为 Q2 销售不及预期。",
        "同意 70 万的方案。Alice 负责周五前出修订版。",
    )
    stt_q = [[TranscriptSegment(text=t, start_ms=0, end_ms=2_000)] for t in transcript]
    diar_q = ["spk-A", "spk-B", "spk-A"]
    pipe = MeetingPipeline(
        settings=s,
        stt=FakeSTT(stt_q),
        diarizer=FakeDiarizer(diar_q),
        rag=rag,
        llm=llm,
    )
    await pipe.start_meeting("mtg-e2e")
    for _ in range(3):
        await pipe.add_audio_chunk("mtg-e2e", b"\x00" * 16_000)

    minutes = await pipe.finalize_meeting("mtg-e2e", title="Q3 预算评审")

    assert "Q3" in minutes.summary or "预算" in minutes.summary
    assert minutes.sections, "至少要有一个 section"
    assert minutes.action_items, "应该提取出 Alice 周五前出修订版的 action item"

    # RAG 入库验证
    hits = await rag.query("Q3 预算", top_k=3)
    assert any("mtg-e2e" in h.doc_id for h in hits)

    await llm.aclose()
