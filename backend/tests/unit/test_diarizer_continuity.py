"""ECAPADiarizer phase4-diar-deep 连续性测试。

修法目标（用户痛点 2026-05-28，最近 2h 实际 3 人 → 40+ unique speaker_id）：
- 跨 chunk 活跃说话人时间窗（同一人在窗内 embed 抖动应合并）
- voiced 段 < diarizer_short_segment_continuity_ms → 归到 last_speaker，不调 ECAPA

本测试用 mock embedding 模拟"真实 ECAPA 在噪音环境下产出"——同一说话人的 embed
跟自己 centroid 的 cos 落在 0.50-0.65（介于活跃宽松阈值 0.35 和主阈值 0.55 之间，
也就是老路径会判新人、新路径在活跃 list 里能合并的关键区间）。
"""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime, timedelta
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


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "diarizer_enabled": True,
        # 让单元测试明确锁住期望值，避免 default 漂移
        "diarizer_match_threshold": 0.55,
        "diarizer_active_match_threshold": 0.35,
        "diarizer_active_window_s": 60.0,
        "diarizer_outlier_match_threshold": 0.50,
        "diarizer_min_voiced_seconds_for_new_profile": 2.0,
        "diarizer_short_segment_continuity_ms": 1500,
    }
    base.update(overrides)
    return Settings(**base)


def _samples_around(
    anchor: np.ndarray,
    n: int,
    *,
    noise_scale: float,
    seed: int,
) -> list[np.ndarray]:
    """模拟"同一说话人 instantaneous embedding"：anchor + 高维各向同性噪声后归一。

    数学性质（anchor 单位长、噪声方向随机均匀）：
      - 同人 sample vs anchor：cos ≈ 1 / sqrt(1 + r²)
      - 同人两 sample 互相：E[cos] ≈ 1 / (1 + r²)
      - 不同人（anchor 正交）两 sample 互相：cos ≈ 0（高维下随机扰动平均掉）

    与 `_drifting_embeddings_around`（threshold_explosion 测试用）的差别：
      那个工具沿不同 perp 方向把每个样本从 anchor 推开，导致两个同人 sample
      互相 cos ≈ target_cos²（64-d 下 [0.50,0.65] target 给出 pairwise ≈ 0.30），
      与 ECAPA 真实 within-speaker 几何不符（实际 ECAPA 同人 pair cos 普遍 ≥ 0.5）。
      本工具的 pairwise cos 跟 anchor-cos 在同一量级，更接近真实。
    """
    rng = np.random.default_rng(seed=seed)
    dim = anchor.shape[0]
    out: list[np.ndarray] = []
    for _ in range(n):
        noise = rng.normal(size=dim).astype(np.float32)
        noise /= float(np.linalg.norm(noise))
        v = anchor + noise_scale * noise
        v = v / float(np.linalg.norm(v))
        out.append(v.astype(np.float32))
    return out


def _three_speaker_interleaved_feed(n_each: int = 10, dim: int = 64) -> list[np.ndarray]:
    """3 个说话人，每人 n_each 段，轮流（A B C A B C ...）。

    noise_scale=0.9 → 同人 pairwise cos ≈ 1/(1+0.81) ≈ 0.55，介于活跃阈值 0.35
    和主阈值 0.55 之间——主阈值未必命中，活跃阈值稳命中（修法核心）。
    三人 anchor 正交化后，跨人 pairwise cos ≈ 0（远低于 0.35）。
    """
    rng = np.random.default_rng(seed=12345)
    anchors: list[np.ndarray] = []
    for _ in range(3):
        a = rng.normal(size=dim).astype(np.float32)
        a /= float(np.linalg.norm(a))
        anchors.append(a)
    a, b, c = anchors
    b = b - b.dot(a) * a
    b /= float(np.linalg.norm(b))
    c = c - c.dot(a) * a - c.dot(b) * b
    c /= float(np.linalg.norm(c))
    a_feed = _samples_around(a, n_each, noise_scale=0.9, seed=101)
    b_feed = _samples_around(b, n_each, noise_scale=0.9, seed=202)
    c_feed = _samples_around(c, n_each, noise_scale=0.9, seed=303)
    feed: list[np.ndarray] = []
    for i in range(n_each):
        feed.append(a_feed[i])
        feed.append(b_feed[i])
        feed.append(c_feed[i])
    return feed


@pytest.mark.asyncio
@pytest.mark.unit
async def test_three_speakers_interleaved_thirty_segments_collapses_to_three_ids() -> None:
    """3 个 speaker × 10 段轮流（30 段）→ 最终 unique speaker_id ≤ 4。

    这是 phase4-diar-deep 的核心回归：老路径会爆出 30+ ID（每段独立 ECAPA + 主
    阈值 0.55 在 [0.50, 0.65] cos 抖动里偶发不命中），新路径活跃窗 0.35 把同人
    所有段绑死。允许 ≤ 4（最差情况：首段 cos 抖到 0.50 边缘短暂分裂，下一段又合回）。
    """
    d = ECAPADiarizer(_settings())
    feed = _three_speaker_interleaved_feed(n_each=10)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    # 30 段，每段 2.5s sine（≥ min_voiced_seconds_for_new_profile=2.0 允许注册新人）
    # 用 identify 接口（内部走 identify_segments → 主导段聚合），每段一个 chunk
    seg_buf = _sine_pcm(2_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        sids: list[str | None] = []
        for _ in range(30):
            sid = await d.identify(seg_buf)
            sids.append(sid)

    assigned = [s for s in sids if s is not None]
    unique = set(assigned)
    assert len(unique) <= 4, f"expected ≤ 4 unique speaker_ids, got {len(unique)}: {unique}"
    # 至少 3 个不同 ID（3 个人本来就该分开）
    assert len(unique) >= 3, f"expected ≥ 3 distinct speakers, got {len(unique)}: {unique}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_single_speaker_twenty_segments_collapses_to_one_id() -> None:
    """单人说 20 段（cos 抖动 [0.50, 0.65]）→ 全部合并到同一 speaker_id。

    锁死活跃窗 + EMA 在长尾上的稳定性。
    """
    d = ECAPADiarizer(_settings())
    rng = np.random.default_rng(seed=42)
    anchor = rng.normal(size=64).astype(np.float32)
    anchor /= float(np.linalg.norm(anchor))
    feed = _samples_around(anchor, n=20, noise_scale=0.9, seed=42)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    seg_buf = _sine_pcm(2_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        sids = []
        for _ in range(20):
            sids.append(await d.identify(seg_buf))

    assigned = [s for s in sids if s is not None]
    assert len(set(assigned)) == 1, f"expected 1 unique speaker, got {set(assigned)}"
    assert len(assigned) == 20, f"expected all 20 segments labeled, got {len(assigned)}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_voiced_segment_attaches_to_last_speaker_without_embed() -> None:
    """voiced 段 < diarizer_short_segment_continuity_ms 且 ctx 有 last_speaker
    → 直接归并，不调 ECAPA embed。

    场景：[A 2.5s | 静 0.3s | 0.9s sine]。0.9s 段满足 audio_gate.min_segment_ms=800
    但 < diarizer_short_segment_continuity_ms=1500 → 应该归到 A，且不触发 embed。
    """
    d = ECAPADiarizer(_settings(diarizer_short_segment_continuity_ms=1500))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    embed_calls = 0

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        nonlocal embed_calls
        embed_calls += 1
        return vec_a

    # 2.5s sine（注册 A）+ 静 300ms + 900ms sine（短段：≥ 800 VAD 下限，< 1500 归并阈值）
    buf = _sine_pcm(2_500) + _silence_pcm(300) + _sine_pcm(900)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assigned = [s.speaker_id for s in out if s.speaker_id is not None]
    assert len(assigned) >= 2, f"expected at least 2 labeled segments, got {out}"
    # 关键断言：所有段都是 A，且 embed 只调了 1 次（短段没 embed）
    assert all(sid == "speaker_1" for sid in assigned), f"expected all → speaker_1, got {assigned}"
    assert embed_calls == 1, f"expected 1 embed call (only long seg), got {embed_calls}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_active_window_expiry_falls_back_to_global_match() -> None:
    """超过 active_window_s 没说话 → 该 speaker 离开活跃 list；下次说话走全局
    严判（_profiles 主阈值 0.55），命中老 ID 算复用。

    场景：A 说 1 段（注册 + 进活跃）→ 模拟 120s 后 A 又说 1 段（embed 与 A
    centroid cos=0.99，远超主阈值 0.55）→ 应命中 speaker_1（复用），不是新人。
    """
    d = ECAPADiarizer(_settings(diarizer_active_window_s=60.0))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_a2 = np.array([0.99, 0.14, 0.0], dtype=np.float32)
    vec_a2 = vec_a2 / float(np.linalg.norm(vec_a2))  # cos(a, a2) ≈ 0.99

    feed = [vec_a, vec_a2]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    seg_buf = _sine_pcm(2_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid1 = await d._identify_one(
            seg_buf,
            16_000,
            dur_sec=2.5,
            voiced_active_s=2.5,
            context_id="_ambient",
            now=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        )
        # 跳过 120s 后再来
        sid2 = await d._identify_one(
            seg_buf,
            16_000,
            dur_sec=2.5,
            voiced_active_s=2.5,
            context_id="_ambient",
            now=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC) + timedelta(seconds=120),
        )

    assert sid1 == "speaker_1"
    assert sid2 == "speaker_1", f"expected reuse via global match, got {sid2}"
    # 注册计数仍是 1（没爆出 speaker_2）
    assert d._counter == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_meeting_contexts_isolate_active_lists() -> None:
    """不同 meeting_id 的活跃 list 互相隔离：m1 的活跃 speaker 不会影响 m2 的匹配。

    场景：在 m1 里注册一个 A（cos 0.4 阈值范围内能被 m1 活跃命中），切到 m2，
    传一个新向量 B（与 A cos≈0.2，远低于活跃阈值 0.35）→ 不会因为 m1 活跃 list
    里有 A 就误判，应该走全局严判 → 全局 _profiles 里有 A（cos 0.2 < 0.55）→
    注册新人 B。
    """
    d = ECAPADiarizer(_settings(diarizer_min_voiced_seconds_for_new_profile=0.5))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    # 与 A cos≈0.2（< 主阈值 0.55 也 < 活跃阈值 0.35）
    vec_b = np.array([0.2, 0.98, 0.0], dtype=np.float32)
    vec_b /= float(np.linalg.norm(vec_b))
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    seg_buf = _sine_pcm(2_500)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid_m1 = await d._identify_one(
            seg_buf,
            16_000,
            dur_sec=2.5,
            voiced_active_s=2.5,
            context_id="m1",
            now=now,
        )
        sid_m2 = await d._identify_one(
            seg_buf,
            16_000,
            dur_sec=2.5,
            voiced_active_s=2.5,
            context_id="m2",
            now=now,
        )

    assert sid_m1 == "speaker_1"
    assert sid_m2 == "speaker_2", f"expected new speaker in isolated m2 ctx, got {sid_m2}"
    # m1 活跃 list 里只有 speaker_1
    assert {a.speaker_id for a in d._contexts["m1"].active} == {"speaker_1"}
    # m2 活跃 list 里只有 speaker_2
    assert {a.speaker_id for a in d._contexts["m2"].active} == {"speaker_2"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_segment_without_last_speaker_still_returns_none() -> None:
    """phase4-diar-deep 短段归并只在 context 已有 last_speaker 时触发；
    第一段就是短段且无历史 → 仍走老路径（embed → 短段门控 → None / 注册）。

    场景：刚启动，第一段就是 1.2s（< short_segment_continuity_ms=1500）。
    embed 走，active 空，global 空，active_s=1.2 < min_for_new=2.0 → 没人可回退 → None。
    """
    d = ECAPADiarizer(_settings(diarizer_short_segment_continuity_ms=1500))
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    embed_calls = 0

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        nonlocal embed_calls
        embed_calls += 1
        return vec_a

    # 单段 1.2s（≥ 800 VAD 下限，< 1500 归并阈值），无前置上下文
    buf = _sine_pcm(1_200)
    with patch.object(d, "_embed", side_effect=_fake_embed):
        out = await d.identify_segments(buf)

    assert len(out) == 1
    # 没有 last_speaker → 走 embed 路径 → 短段门控 + 无可回退 → None
    assert out[0].speaker_id is None, f"expected None (no last_speaker), got {out[0].speaker_id}"
    assert embed_calls == 1, "expected embed actually called (no continuity shortcut)"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_settings_have_phase4_diar_deep_values() -> None:
    """锁死 phase4-diar-deep settings 的 default，防被无意改回。"""
    s = Settings()
    assert s.diarizer_active_window_s == 60.0
    assert s.diarizer_active_match_threshold == 0.35
    assert s.diarizer_short_segment_continuity_ms == 1500
    # 用户 2026-06 ambient 编号爆炸修复：cap 6 → 4（典型自由对话 1-3 人）
    assert s.diarizer_ambient_max_speakers == 4


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ambient_max_speakers_cap_forces_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户 2026-05-28：ambient 长时间运行编号爆到 11/19（实际 3 人）。

    cap=3 时，前 3 段都互相正交（cos≈0）→ 各注册新人，每次都注入 active list。
    第 4 段又一组新正交向量进来：
    - 阶段 1 active 匹配（阈值 0.35）：active 里 3 人 cos 都 < 0.35，全 miss
    - 阶段 2 global _profiles 匹配（阈值 0.55）：同上 miss
    - 阶段 3 voiced_active_s ≥ min_for_new=0.5 本来要 _counter+=1 注册 speaker_4
    - 用户修法：ambient cap 命中 → 强制复用 best_sim 最高的现有 ID（即使 sim 低）
    断言：30 段不同正交向量，unique 数 ≤ cap=3，_counter 也卡在 3。
    """
    cap = 3
    d = ECAPADiarizer(
        _settings(
            diarizer_min_voiced_seconds_for_new_profile=0.5,
            diarizer_ambient_max_speakers=cap,
            # 把活跃窗拉长到 1h，避免测试里被时间窗清理掉
            diarizer_active_window_s=3600.0,
        )
    )

    rng = np.random.default_rng(seed=7)
    dim = 64
    feed: list[np.ndarray] = []
    for _ in range(30):
        v = rng.normal(size=dim).astype(np.float32)
        v /= float(np.linalg.norm(v))
        feed.append(v)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    seg_buf = _sine_pcm(2_500)
    sids: list[str | None] = []
    with patch.object(d, "_embed", side_effect=_fake_embed):
        for _ in range(30):
            sid = await d._identify_one(
                seg_buf,
                16_000,
                dur_sec=2.5,
                voiced_active_s=2.5,
                context_id="_ambient",
                now=now,
            )
            sids.append(sid)

    assigned = {s for s in sids if s is not None}
    assert len(assigned) <= cap, f"ambient cap broken: {len(assigned)} > {cap}, got {assigned}"
    # _counter 不再无限累积（前 cap 段分配 speaker_1..speaker_3 之后不再 +1）
    assert d._counter == cap, f"counter ran past cap: {d._counter} != {cap}"
