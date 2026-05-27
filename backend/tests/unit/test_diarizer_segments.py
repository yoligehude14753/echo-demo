"""ECAPADiarizer.identify_segments 单测（PR echodesk-spk-2）。

核心：单 6s chunk 内多人混音场景下，每个 voiced 段独立 embed + match。
mock `_embed` 返回不同向量来模拟"两个说话人"。

测的不是 ECAPA 模型本身（用 mock 替代），是切片 + 逐段聚合的链路。
"""

from __future__ import annotations

import math
import struct
from unittest.mock import patch

import numpy as np
import pytest
from app.adapters.diarizer import ECAPADiarizer
from app.adapters.diarizer.ecapa import SegmentSpeaker
from app.config import Settings


def _sine_pcm(duration_ms: int, amplitude: int = 4000, freq_hz: int = 440) -> bytes:
    n = int(16_000 * duration_ms / 1000)
    samples = [
        int(amplitude * math.sin(2 * math.pi * freq_hz * i / 16_000))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def _silence_pcm(duration_ms: int) -> bytes:
    n = int(16_000 * duration_ms / 1000)
    return b"\x00" * (n * 2)


def _settings() -> Settings:
    # 用偏宽松的 threshold 配合 mock vec 的设计
    return Settings(diarizer_enabled=True, diarizer_match_threshold=0.65)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_identify_segments_returns_empty_on_silence() -> None:
    d = ECAPADiarizer(_settings())
    out = await d.identify_segments(_silence_pcm(6_000))
    assert out == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_identify_segments_single_voiced_returns_one() -> None:
    d = ECAPADiarizer(_settings())
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(_sine_pcm(3_000))
    assert len(out) == 1
    assert isinstance(out[0], SegmentSpeaker)
    assert out[0].speaker_id == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_two_speakers_in_one_chunk_get_distinct_ids() -> None:
    """[A 1.5s | 静 0.5s | B 1.5s] → 两段，不同 speaker_id。

    这就是 spk-2 要修的根因场景：之前整段 embed → 混合向量 → 都不像 → 注册新人。
    """
    d = ECAPADiarizer(_settings())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(1_500, freq_hz=440) + _silence_pcm(500) + _sine_pcm(
        1_500, freq_hz=880
    )
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert len(out) == 2
    ids = [s.speaker_id for s in out]
    assert ids == ["speaker_1", "speaker_2"]
    assert out[0].duration_ms >= 1_400 and out[1].duration_ms >= 1_400


@pytest.mark.asyncio
@pytest.mark.unit
async def test_same_speaker_twice_in_chunk_returns_same_id() -> None:
    """[A 1.5s | 静 0.5s | A 1.5s] → 两段，同一 speaker_id（EMA 命中）。"""
    d = ECAPADiarizer(_settings())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_a2 = np.array([0.95, 0.05, 0.0], dtype=np.float32)
    vec_a2 = vec_a2 / float(np.linalg.norm(vec_a2))  # cos≈0.999

    feed = [vec_a, vec_a2]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(1_500) + _silence_pcm(500) + _sine_pcm(1_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert len(out) == 2
    assert out[0].speaker_id == out[1].speaker_id == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_identify_fallback_returns_dominant_segment_id() -> None:
    """老 identify 接口走切片 → 选最长段的 speaker。

    [A 短 1s | 静 0.5s | B 长 2.5s] → 返回 B 的 id。
    """
    d = ECAPADiarizer(_settings())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(1_000) + _silence_pcm(500) + _sine_pcm(2_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid = await d.identify(buf)
    # B 是 speaker_2（A 先注册成 speaker_1）；最长段是 B
    assert sid == "speaker_2"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_identify_segments_short_buffer_returns_empty() -> None:
    d = ECAPADiarizer(_settings())
    # < _MIN_BYTES_FOR_EMBED=32_000
    assert await d.identify_segments(b"\x00" * 16_000) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_disabled_diarizer_returns_empty_segments() -> None:
    d = ECAPADiarizer(Settings(diarizer_enabled=False))
    assert await d.identify_segments(_sine_pcm(3_000)) == []
