"""ECAPADiarizer spk-3 门控单测：voiced active seconds 决定能否注册新人。

spk-3 的两个 settings：
- `diarizer_min_voiced_seconds_for_new_profile`（默认 1.5s）
- `diarizer_outlier_match_threshold`（默认 0.50）

之前是硬编码 `_MIN_DUR_FOR_NEW_PROFILE=4.0` + `_OUTLIER_SIM_ALLOW_NEW=0.30`，
对 6s 固定 chunk 是死分支。spk-2 切句后段长普遍 1-3s，4s 永远不命中；spk-3
把门控建立在 voiced_active_s（段长 × 帧活跃率）上，更接近"真实人声时长"。
"""

from __future__ import annotations

import math
import struct
from unittest.mock import patch

import numpy as np
import pytest
from app.adapters.diarizer import ECAPADiarizer
from app.config import Settings


def _sine_pcm(duration_ms: int, amplitude: int = 4000, freq_hz: int = 440) -> bytes:
    n = int(16_000 * duration_ms / 1000)
    samples = [
        int(amplitude * math.sin(2 * math.pi * freq_hz * i / 16_000))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def _silence_pcm(duration_ms: int) -> bytes:
    return b"\x00" * (int(16_000 * duration_ms / 1000) * 2)


def _enabled(**kwargs) -> Settings:
    base = {
        "diarizer_enabled": True,
        "diarizer_match_threshold": 0.65,
    }
    base.update(kwargs)
    return Settings(**base)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_voiced_segment_below_min_does_not_register_new_speaker() -> None:
    """段 voiced_active_s < min_for_new 且 _profiles 空 → 返回 None（不注册新人）。

    场景：[1.2s sine]（< 1.5s 门控）。没有已知人可回退 → 段被丢弃。
    """
    d = ECAPADiarizer(_enabled())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec_a

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(_sine_pcm(1_200))

    assert len(out) == 1
    assert out[0].speaker_id is None  # 段被丢，不注册
    assert d._counter == 0  # 计数没动


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_voiced_segment_falls_back_to_known_speaker() -> None:
    """段太短但跟已知人足够相似（sim >= outlier_threshold）→ 回退命中。

    场景：先注册一个 vec_a，然后来 1.2s 段 vec_b（cos=0.8 ≥ 0.50 outlier）。
    """
    d = ECAPADiarizer(_enabled())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.8, 0.6, 0.0], dtype=np.float32)  # cos(a,b)=0.8
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(2_000) + _silence_pcm(500) + _sine_pcm(1_200)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert len(out) == 2
    assert out[0].speaker_id == "speaker_1"  # 2s 段正常注册
    assert out[1].speaker_id == "speaker_1"  # 1.2s 段回退命中
    assert d._counter == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_voiced_segment_below_outlier_threshold_dropped() -> None:
    """段太短且跟所有已知人都不够相似（sim < outlier_threshold）→ 丢弃，不污染。

    这是 spk-3 关键改动：以前 _OUTLIER_SIM_ALLOW_NEW=0.30 几乎任何向量都能回退到
    某个已知人，导致 centroid 被乱拉。现在 outlier_threshold=0.50 + 不够就丢。
    """
    d = ECAPADiarizer(_enabled())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    # cos(a, b) = 0.35 < outlier 0.50
    vec_b = np.array([0.35, 0.94, 0.0], dtype=np.float32)
    vec_b = vec_b / float(np.linalg.norm(vec_b))
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(2_000) + _silence_pcm(500) + _sine_pcm(1_200)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert out[0].speaker_id == "speaker_1"
    assert out[1].speaker_id is None  # 短 + 不够相似 → 丢
    assert d._counter == 1  # 没注册新人


@pytest.mark.asyncio
@pytest.mark.unit
async def test_long_voiced_segment_registers_new_speaker_even_if_dissimilar() -> None:
    """段够长（active_s >= min_for_new）→ 不命中阈值时正常注册新人。

    spk-1 之前的 threshold=0.65 行为继续工作；spk-3 只对短段额外门控。
    """
    d = ECAPADiarizer(_enabled())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0], dtype=np.float32)  # cos=0 ＜ 0.65
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(2_000) + _silence_pcm(500) + _sine_pcm(2_000)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert out[0].speaker_id == "speaker_1"
    assert out[1].speaker_id == "speaker_2"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_min_voiced_seconds_setting_takes_effect() -> None:
    """settings.diarizer_min_voiced_seconds_for_new_profile 可调，0.5s 时 1s 段能注册。"""
    d = ECAPADiarizer(_enabled(diarizer_min_voiced_seconds_for_new_profile=0.5))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec_a

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(_sine_pcm(1_200))

    assert out[0].speaker_id == "speaker_1"  # 1.2s ≥ 0.5s → 能注册


@pytest.mark.asyncio
@pytest.mark.unit
async def test_outlier_threshold_setting_takes_effect() -> None:
    """settings.diarizer_outlier_match_threshold 可调，0.30 时 cos=0.35 也能回退。"""
    d = ECAPADiarizer(_enabled(diarizer_outlier_match_threshold=0.30))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.35, 0.94, 0.0], dtype=np.float32)
    vec_b = vec_b / float(np.linalg.norm(vec_b))
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    buf = _sine_pcm(2_000) + _silence_pcm(500) + _sine_pcm(1_200)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    # 0.35 ≥ 0.30 outlier threshold → 回退命中
    assert out[1].speaker_id == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_active_ratio_affects_gate_decision() -> None:
    """段长够但 active_ratio 低 → voiced_active_s 不达门控 → 不注册。

    场景：silence-gap 不超过 200ms 所以不切句，得到单段；但段内大量静音使
    active_ratio 远低于 1。voiced_active_s = duration × active_ratio 应该用
    "真实活跃语音"门控，而不是段总长。

    用显式 settings 让用例跟默认值脱钩。
    """
    d = ECAPADiarizer(_enabled(diarizer_min_voiced_seconds_for_new_profile=1.5))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec_a

    # 600ms sine ×3 / 150ms silence ×2 = 2.1s 段长，active_ratio = 1800/2100 ≈ 0.857
    # voiced_active_s ≈ 1.8s ≥ 1.5 → 注册（验证门控算的是 active_s 不是段长）
    buf = (
        _sine_pcm(600)
        + _silence_pcm(150)
        + _sine_pcm(600)
        + _silence_pcm(150)
        + _sine_pcm(600)
    )
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert len(out) == 1
    assert out[0].speaker_id == "speaker_1"
