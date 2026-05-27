"""单测：audio_gate.split_into_voiced_segments（PR echodesk-spk-2 新增）。

这个工具是 spk-2 的核心：把一个 6s ambient chunk 按 silence-gap 切成多个
voiced 段，让 ECAPA 在"两个人混在一个 chunk"的场景下分别 embed → 跟各自
profile 匹配，而不是被混合向量送进 _best_match 然后被判新人。

注意：测试只覆盖切片算法本身（无外部依赖），diarizer 集成在 test_diarizer_adapter.py。
"""

from __future__ import annotations

import math
import struct

import pytest

from app.adapters.audio_gate import VoicedSegment, split_into_voiced_segments


def _sine_pcm(duration_ms: int, amplitude: int = 4000, freq_hz: int = 440) -> bytes:
    """合成 int16 mono 16kHz 正弦波（用于模拟"语音"）。

    amp=4000 → RMS ≈ 2830，远高于默认 frame_rms_threshold=400。
    """
    n = int(16_000 * duration_ms / 1000)
    samples = [
        int(amplitude * math.sin(2 * math.pi * freq_hz * i / 16_000))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def _silence_pcm(duration_ms: int) -> bytes:
    n = int(16_000 * duration_ms / 1000)
    return b"\x00" * (n * 2)


def _concat(*chunks: bytes) -> bytes:
    return b"".join(chunks)


def test_all_silence_returns_empty() -> None:
    buf = _silence_pcm(6_000)  # 6s 全静音
    segs = split_into_voiced_segments(buf)
    assert segs == []


def test_all_voiced_returns_single_segment() -> None:
    buf = _sine_pcm(3_000)
    segs = split_into_voiced_segments(buf)
    assert len(segs) == 1
    assert segs[0].duration_ms >= 2_800
    assert segs[0].active_ratio > 0.95


def test_voice_silence_voice_returns_two_segments() -> None:
    """[1s 语音 | 0.5s 静音 | 1s 语音] → 2 段。

    这是 spk-2 要解决的核心场景：单 chunk 里两人交替说话。
    """
    buf = _concat(
        _sine_pcm(1_000, freq_hz=440),
        _silence_pcm(500),
        _sine_pcm(1_000, freq_hz=880),
    )
    segs = split_into_voiced_segments(buf)
    assert len(segs) == 2
    first, second = segs
    assert first.start_ms < first.end_ms <= 1_100
    assert second.start_ms >= 1_400
    assert second.end_ms <= 2_600


def test_short_voiced_below_min_dropped() -> None:
    """0.3s 语音段 < min_segment_ms=800ms → 丢弃。

    防止把 0.2s 拍门声、咳嗽这种短脉冲当作"voiced 段"塞给 ECAPA。
    """
    buf = _concat(
        _sine_pcm(300),
        _silence_pcm(1_500),
        _sine_pcm(200),
    )
    segs = split_into_voiced_segments(buf)
    assert segs == []


def test_short_gap_under_threshold_merges_into_one() -> None:
    """[1s 语音 | 0.1s 静音 | 1s 语音] → 1 段（gap < max_silence_gap_ms=200ms）。

    自然说话里的换气停顿就在 100-200ms 量级，不能切。
    """
    buf = _concat(
        _sine_pcm(1_000),
        _silence_pcm(100),
        _sine_pcm(1_000),
    )
    segs = split_into_voiced_segments(buf)
    assert len(segs) == 1
    assert segs[0].duration_ms >= 1_900


def test_trailing_voiced_segment_emitted() -> None:
    """末尾还在 voiced 状态时也要 emit 一段（不能因没遇到 silence 就丢）。"""
    buf = _concat(
        _silence_pcm(500),
        _sine_pcm(1_500),
    )
    segs = split_into_voiced_segments(buf)
    assert len(segs) == 1
    assert segs[0].start_ms >= 400
    assert segs[0].active_ratio > 0.9


def test_buffer_too_small_returns_empty() -> None:
    """< 1 帧 → 返回 []（防越界）。"""
    assert split_into_voiced_segments(b"") == []
    assert split_into_voiced_segments(b"\x00" * 100) == []


def test_audio_bytes_slice_aligns_with_offsets() -> None:
    """切出来的 audio_bytes 长度应与 (end_ms - start_ms) 对得上（16kHz int16）。"""
    buf = _concat(
        _silence_pcm(500),
        _sine_pcm(1_500),
        _silence_pcm(500),
        _sine_pcm(1_500),
    )
    segs = split_into_voiced_segments(buf)
    assert len(segs) == 2
    for seg in segs:
        expected_bytes = (seg.end_ms - seg.start_ms) * 16_000 // 1000 * 2
        assert len(seg.audio_bytes) == expected_bytes


@pytest.mark.parametrize(
    "frame_rms_threshold,expected_count",
    [
        (400, 2),   # 默认阈值能识别两段
        (10_000, 0),  # 阈值高过 sin 波 RMS，无段
    ],
)
def test_threshold_parameter_takes_effect(
    frame_rms_threshold: int, expected_count: int
) -> None:
    buf = _concat(_sine_pcm(1_000), _silence_pcm(500), _sine_pcm(1_000))
    segs = split_into_voiced_segments(
        buf, frame_rms_threshold=frame_rms_threshold
    )
    assert len(segs) == expected_count
