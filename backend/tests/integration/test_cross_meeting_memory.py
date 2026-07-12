"""PR-13: 跨会议记忆 E2E。

链路：
- 会议 A inject_segment → finalize → 自动 RAG ingest
- /rag/ask 走 retrieve_and_answer → 应能从 A 的 transcript / summary 命中引用

依赖：
- Yunwu LLM 可达
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.retrieve_and_answer import retrieve_and_answer


def _yunwu_alive() -> bool:
    if not os.getenv("YUNWU_OPEN_KEY"):
        return False
    try:
        with socket.create_connection(("yunwu.ai", 443), timeout=3):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(not _yunwu_alive(), reason="Yunwu LLM 不可达"),
]


class _NullEventBus:
    async def publish(self, *_a: object, **_kw: object) -> None:
        pass


class _NoopWebSearch:
    """跨会议记忆测试不要走真 web search，避免噪音。"""

    async def search(self, *_a: object, **_kw: object) -> list[Any]:
        return []


class _NoopDiarizer:
    """跨会议记忆测试无音频，diarize 走 noop。"""

    async def identify(self, *_a: object, **_kw: object) -> str | None:
        return None

    async def reset(self) -> None:
        pass


class _NoopSTT:
    """跨会议记忆测试不走 STT，确保 init 类型对齐。"""

    async def transcribe(self, *_a: object, **_kw: object) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_cross_meeting_memory_recall(tmp_path: Path) -> None:
    """会议 A 提及 'NVIDIA H100 价格 30000 美元'，
    问会议 B 时通过 RAG 召回会议 A 的内容（B 也可以是新会话）。
    """
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skill",
        diarizer_enabled=False,
    )
    llm = OpenAICompatibleLLM(settings)
    rag = BM25Rag(settings)
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_NoopSTT(),  # type: ignore[arg-type]
        diarizer=_NoopDiarizer(),
        rag=rag,
        llm=llm,
        event_bus=_NullEventBus(),  # type: ignore[arg-type]
    )

    # ── 会议 A：芯片采购讨论 ──────────────────────────────
    meeting_a = "memory-meeting-a"
    await pipeline.start_meeting(meeting_a)

    # 注入 segment（绕过 STT）
    segs_a = [
        TranscriptSegment(
            meeting_id=meeting_a,
            start_ms=0,
            end_ms=8_000,
            text="我们今天讨论 GPU 采购预算。NVIDIA H100 当前市场价 30000 美元一张。",
            speaker_label="说话人1",
        ),
        TranscriptSegment(
            meeting_id=meeting_a,
            start_ms=8_000,
            end_ms=16_000,
            text="对，H100 比上一代 A100 贵 60%。我们这次的预算只能买 8 张。",
            speaker_label="说话人2",
        ),
        TranscriptSegment(
            meeting_id=meeting_a,
            start_ms=16_000,
            end_ms=24_000,
            text="那总成本就是 240000 美元，财务那边需要走 OA 流程。",
            speaker_label="说话人1",
        ),
    ]
    for seg in segs_a:
        await pipeline.append_segment(meeting_a, seg)
    await pipeline.finalize_meeting(meeting_a, title="GPU 采购预算会议")

    # ── 会议 B：起一个新会话来问 ──────────────────────────
    # 用本地知识库友好的 question，引导 classifier 走 rag
    # 同时也证明：用户问"上次会议讨论过什么"时，能命中已结束会议的 RAG ingest
    result = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model=settings.llm_fast_model,
        rag=rag,
        web=_NoopWebSearch(),  # type: ignore[arg-type]
        question="上次会议讨论的 GPU 采购预算里，NVIDIA H100 的单价和总预算是多少？",
        rag_top_k=5,
        web_top_n=0,
    )

    # 收集流式输出
    answer_parts: list[str] = []
    async for chunk in result.chunks:
        answer_parts.append(chunk)
    answer = "".join(answer_parts)

    # ── 校验 ───────────────────────────────────────────────
    # 1. RAG 应至少召回 1 个 chunk
    assert len(result.retrieval.rag_chunks) >= 1, f"RAG 没召回任何 chunk: {result.retrieval}"
    # 2. 召回 chunk 的 doc_id 应来自会议 A
    rag_doc_ids = {c.doc_id for c in result.retrieval.rag_chunks}
    assert any(meeting_a in d for d in rag_doc_ids), (
        f"RAG 召回的 doc_id 不来自会议 A: {rag_doc_ids}"
    )
    # 3. 答案应包含 H100 价格信息（30000 / 美元 / NVIDIA 任一）
    assert any(kw in answer for kw in ("30000", "30,000", "三万", "H100")), (
        f"答案没引用会议 A 的关键信息: {answer[:300]}"
    )

    await llm.aclose()
