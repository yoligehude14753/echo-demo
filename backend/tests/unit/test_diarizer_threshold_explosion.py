"""Speaker explosion 回归：text-clarity PR 把 threshold 0.70 → 0.55。

用户痛点（2026-05-28，会议 m-bdd1da4e7e21）：3 个真人说话被分成 14 个 ID。

本测试用 mock embedding 对照"老 0.70 阈值"和"新 0.55 阈值"在同一份"intra-speaker
抖动"输入下的行为差异：
- 老：~5 段都判新人 → 5 个 speaker_id
- 新：5 段合并成 1 个 speaker_id（命中 EMA centroid）

我们 mock 一个真实场景：同一说话人 A 跨 5 个 voiced 段，每段 embedding 与上一段
cos ≈ 0.60-0.68（典型 intra-speaker drift）。
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
    samples = [int(amplitude * math.sin(2 * math.pi * freq_hz * i / 16_000)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _silence_pcm(duration_ms: int) -> bytes:
    return b"\x00" * (int(16_000 * duration_ms / 1000) * 2)


def _make_drifting_embeddings(
    n: int,
    cos_to_anchor_lo: float,
    cos_to_anchor_hi: float,
    dim: int = 64,
) -> list[np.ndarray]:
    """生成 n 个向量：v_0 是 anchor；v_1..v_{n-1} 每个与 v_0 的 cos 都在 [lo, hi]。

    模拟同一说话人跨 chunk 的 embedding drift：第一段定义"参考"（注册为
    centroid），后续段每段与该 centroid 的 cos 都落在指定区间——这就是 ECAPA
    要判定"是否同一人"时实际看到的几何。

    实现：用不同的随机正交方向把后续向量从 anchor 上"推开"到指定 cos；
    不同段之间的 cos 不受控（也不需要，diarizer 只看 vs centroid）。
    """
    rng = np.random.default_rng(seed=42)
    anchor = rng.normal(size=dim).astype(np.float32)
    anchor = anchor / float(np.linalg.norm(anchor))

    out: list[np.ndarray] = [anchor]
    for _ in range(n - 1):
        target_cos = float(rng.uniform(cos_to_anchor_lo, cos_to_anchor_hi))
        sin = math.sqrt(max(0.0, 1.0 - target_cos**2))
        # 每段一个全新的正交方向（模拟独立的"姿态/距离/相位"扰动）
        perp = rng.normal(size=dim).astype(np.float32)
        perp -= perp.dot(anchor) * anchor
        perp /= float(np.linalg.norm(perp))
        v = target_cos * anchor + sin * perp
        v = v / float(np.linalg.norm(v))
        out.append(v.astype(np.float32))
    return out


def _settings(threshold: float, outlier: float = 0.50) -> Settings:
    return Settings(
        diarizer_enabled=True,
        diarizer_match_threshold=threshold,
        diarizer_outlier_match_threshold=outlier,
        diarizer_min_voiced_seconds_for_new_profile=2.0,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_old_strict_threshold_creates_speaker_explosion() -> None:
    """对照组：旧 threshold=0.70 → 同一说话人的 5 段被切成 5 个 speaker。

    这就是会议 m-bdd1da4e7e21 看到的现象——本测试锁住"老配置的爆炸行为"，
    确保下面新 threshold 测试的 fix 不是侥幸。
    """
    d = ECAPADiarizer(_settings(threshold=0.70))
    # 5 段 same-speaker：第一段定义 anchor，后续 4 段与 anchor 的 cos ∈ [0.60, 0.68]
    # （典型 intra-speaker drift；都 < 0.70 阈值 → 老配置后续每段都判新人）
    embs = _make_drifting_embeddings(n=5, cos_to_anchor_lo=0.60, cos_to_anchor_hi=0.68)
    feed = list(embs)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    # 5 段 2.5s sine，中间 0.4s 静音断句（< 0.5s 仍切句）
    seg = _sine_pcm(2_500) + _silence_pcm(400)
    buf = seg * 5

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    sids = {s.speaker_id for s in out if s.speaker_id}
    # 严格阈值 → 每段都被判新人（≥ 3 个，演示 explosion；不锁死 5 因 vad 可能合并）
    assert len(sids) >= 3, f"expected speaker explosion under threshold=0.70, got {sids}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_new_loose_threshold_merges_same_speaker_segments() -> None:
    """主张：新 threshold=0.55 把相同输入合并成 1 个 speaker_id。

    这是 text-clarity PR 关键回归测试。
    """
    d = ECAPADiarizer(_settings(threshold=0.55))
    # 同样的输入（cos to anchor ∈ [0.60, 0.68]），新阈值 0.55 < 0.60 → 应全部命中
    embs = _make_drifting_embeddings(n=5, cos_to_anchor_lo=0.60, cos_to_anchor_hi=0.68)
    feed = list(embs)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    seg = _sine_pcm(2_500) + _silence_pcm(400)
    buf = seg * 5

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    sids = {s.speaker_id for s in out if s.speaker_id}
    # 新阈值 → 至少 4/5 段合并到同 1 个 speaker（允许首段建档后 EMA 漂移把
    # 中间某段瞬时 cos 推到边缘，但绝不应再爆出 ≥3 个 ID）
    assert len(sids) <= 2, f"expected 1-2 merged speakers under threshold=0.55, got {sids}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_genuinely_distinct_speakers_still_separate_under_new_threshold() -> None:
    """新阈值 0.55 不会把真正不同的人合并：

    场景：A（[1,0,0]）和 B（[0,0,1]），cos = 0 << 0.55 → 应分别得到不同 ID。
    """
    d = ECAPADiarizer(_settings(threshold=0.55))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    feed = [vec_a, vec_b, vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    seg_a = _sine_pcm(2_100) + _silence_pcm(300)
    seg_b = _sine_pcm(2_100, freq_hz=880) + _silence_pcm(300)
    buf = seg_a + seg_b + seg_a + seg_b

    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    sids = [s.speaker_id for s in out if s.speaker_id]
    # 4 段 → 应得 2 个不同 ID（A、B、A、B）
    assert len(set(sids)) == 2, f"expected 2 distinct speakers, got {set(sids)}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_settings_uses_loosened_threshold() -> None:
    """锁死 settings default：保护 0.55 不被无意改回。"""
    s = Settings()
    assert s.diarizer_match_threshold == 0.55, (
        "default threshold should be 0.55 after text-clarity PR; see config.py threshold 演进史"
    )
    assert s.diarizer_outlier_match_threshold == 0.50, (
        "outlier threshold should be 0.50 (lower than match) after text-clarity PR"
    )
