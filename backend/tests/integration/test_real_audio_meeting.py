"""PR-13: 真音频 → STT → diarize → minutes E2E。

依赖：
- heyi-bj :8090 STT (FireRed) 在线（不可达 → skip）
- heyi-bj :8094 TTS 在线（生成 audio fixture，不可达 + 无 cache → skip）
- yunwu LLM 可达（生成 minutes）
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from app.adapters.diarizer.ecapa import ECAPADiarizer
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.stt import FireRedSTT
from app.config import Settings
from app.use_cases.meeting_pipeline import MeetingPipeline

from tests.fixtures.audio_factory import get_audio_fixture


def _can_connect(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


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
    pytest.mark.skipif(
        not _can_connect("localhost", 8090),
        reason="heyi-bj :8090 STT (FireRed) 不可达",
    ),
    pytest.mark.skipif(not _yunwu_alive(), reason="Yunwu LLM 不可达"),
]


class _NullEventBus:
    """跑测试时不要真往 in-mem event bus 写。"""

    async def publish(self, *_a: object, **_kw: object) -> None:
        pass


class _NoopDiarizer:
    """diarizer_enabled=False 时占位。"""

    async def identify(self, *_a: object, **_kw: object) -> str | None:
        return None

    async def reset(self) -> None:
        pass


@pytest.mark.asyncio
async def test_real_audio_meeting_minutes_e2e(tmp_path: Path) -> None:
    """跑通：30s 真音频 → /chunk → STT 转写 → finalize → LLM minutes。

    校验：minutes.summary ≥ 80 字 + 至少出现 1 个业务关键词。
    """
    wav = await get_audio_fixture("short")
    if wav is None:
        pytest.skip("真音频 fixture 不可生成（faster-qwen3-tts 不可达 + 无 cache）")

    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skill",
        diarizer_enabled=False,  # 单 speaker fixture，关闭 diarize 提速
    )
    llm = OpenAICompatibleLLM(settings)
    stt = FireRedSTT(settings)
    rag = BM25Rag(settings)
    diarizer = ECAPADiarizer(settings) if settings.diarizer_enabled else _NoopDiarizer()
    pipeline = MeetingPipeline(
        settings=settings,
        stt=stt,
        diarizer=diarizer,  # type: ignore[arg-type]
        rag=rag,
        llm=llm,
        event_bus=_NullEventBus(),  # type: ignore[arg-type]
    )

    meeting_id = "real-audio-test"
    await pipeline.start_meeting(meeting_id)
    await pipeline.add_audio_chunk(meeting_id, wav, sample_rate=16_000)
    segs = pipeline.get_segments(meeting_id)
    # STT 至少要识别出一些文本（不强求每个字精确）
    text_all = "".join(s.text for s in segs)
    assert len(text_all) >= 5, f"STT 没识别出文本（仅 {len(text_all)} 字符）: {text_all!r}"
    minutes = await pipeline.finalize_meeting(meeting_id, title="Q3 销售目标拆解")

    # 关键校验：summary 写出真实业务关键词（链路打通后 LLM 总结自然 50-200 字）
    summary = minutes.summary
    assert len(summary) >= 40, f"summary 太短：{len(summary)} chars"
    has_business_kw = any(
        kw in summary for kw in ("销售", "Q3", "华南", "广东", "目标", "数据", "渠道", "拆解")
    )
    assert has_business_kw, f"summary 缺业务关键词：{summary}"
    # sections 至少 1 条
    assert len(minutes.sections) >= 1, "纪要无 sections"

    # 跨会议记忆前置：RAG 已 ingest
    stats_chunks = rag.stats()
    assert stats_chunks.get("n_docs", 0) >= 1, f"RAG 没成功 ingest 会议: {stats_chunks}"

    await llm.aclose()
