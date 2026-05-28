"""AutoMeetingDetector 单测：触发 / 静默结束 / cooldown / 手动让步。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.use_cases.auto_meeting_detector import AutoMeetingDetector

T0 = datetime(2026, 5, 27, 10, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.mark.unit
def test_single_speaker_does_not_trigger() -> None:
    det = AutoMeetingDetector(min_distinct_speakers=2, min_active_seconds=6.0)
    for i in range(10):
        evs = det.observe(
            speaker_id="spk_A",
            duration_ms=3_000,
            now=_at(i * 3),
        )
        assert evs == []
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_two_speakers_with_enough_active_triggers_start() -> None:
    det = AutoMeetingDetector(min_distinct_speakers=2, min_active_seconds=6.0)
    # A 4s + B 4s = 8s, distinct=2 → 触发
    e1 = det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    assert e1 == []
    e2 = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert len(e2) == 1
    assert e2[0].kind == "start"
    assert e2[0].meeting_id.startswith("auto-")
    assert det.active_meeting_id == e2[0].meeting_id


@pytest.mark.unit
def test_two_speakers_below_threshold_does_not_trigger() -> None:
    det = AutoMeetingDetector(min_active_seconds=10.0)
    # 总 active 2s + 2s = 4s < 10s
    det.observe(speaker_id="spk_A", duration_ms=2_000, now=_at(0))
    evs = det.observe(speaker_id="spk_B", duration_ms=2_000, now=_at(1))
    assert evs == []


@pytest.mark.unit
def test_window_prunes_old_speakers() -> None:
    det = AutoMeetingDetector(window_s=30.0, min_active_seconds=6.0)
    # 第一次 A 在 t=0，过了 31s 已淘汰
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    # 31s 后 B 出现，A 已过期，distinct 仍 == 1 → 不触发
    evs = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(31))
    assert evs == []
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_silence_timeout_ends_auto_meeting() -> None:
    det = AutoMeetingDetector(silence_timeout_s=30.0, min_active_seconds=6.0)
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"

    # 31s 后没有任何 chunk → 静默结束
    evs = det.observe(speaker_id=None, duration_ms=0, now=_at(40))
    assert any(e.kind == "end" and e.reason == "silence_timeout" for e in evs)
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_cooldown_prevents_immediate_retrigger() -> None:
    det = AutoMeetingDetector(silence_timeout_s=10.0, min_active_seconds=6.0, cooldown_s=60.0)
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    # 11s 后静默结束
    det.observe(speaker_id=None, duration_ms=0, now=_at(16))
    assert det.active_meeting_id is None

    # cooldown 内即使 2 个说话人也不触发（注意要重置 window 因为内存里 A/B 已 prune 了）
    e1 = det.observe(speaker_id="spk_C", duration_ms=4_000, now=_at(20))
    e2 = det.observe(speaker_id="spk_D", duration_ms=4_000, now=_at(22))
    assert e1 == [] and e2 == []
    assert det.active_meeting_id is None

    # 但 cooldown 之外就可以触发
    e3 = det.observe(speaker_id="spk_E", duration_ms=4_000, now=_at(85))
    e4 = det.observe(speaker_id="spk_F", duration_ms=4_000, now=_at(86))
    assert any(e.kind == "start" for e in e3 + e4)


@pytest.mark.unit
def test_manual_meeting_yields_auto_and_ends_existing() -> None:
    det = AutoMeetingDetector(min_active_seconds=6.0)
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"
    auto_mid = start[0].meeting_id

    # 用户 @开始会议 → manual_meeting_id 注入，detector 让步并 end 自己
    evs = det.observe(
        speaker_id="spk_C",
        duration_ms=3_000,
        now=_at(10),
        manual_meeting_id="user-mtg-1",
    )
    assert any(e.kind == "end" and e.meeting_id == auto_mid for e in evs)
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_no_trigger_while_manual_in_progress() -> None:
    det = AutoMeetingDetector(min_active_seconds=4.0)
    # 用户已 @开始；detector 应保持安静
    det.observe(
        speaker_id="spk_A",
        duration_ms=3_000,
        now=_at(0),
        manual_meeting_id="user-mtg-1",
    )
    evs = det.observe(
        speaker_id="spk_B",
        duration_ms=3_000,
        now=_at(2),
        manual_meeting_id="user-mtg-1",
    )
    assert evs == []
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_force_end_returns_event_only_if_active() -> None:
    det = AutoMeetingDetector(min_active_seconds=6.0)
    # idle → force_end no-op
    assert det.force_end(now=_at(0)) is None
    # 触发后 force_end
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"
    ev = det.force_end(now=_at(10), reason="user_quit")
    assert ev is not None and ev.kind == "end" and ev.reason == "user_quit"
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_silence_does_not_end_after_speaker_resumes() -> None:
    # 2026-05 phase4-meeting-deadlock：硬上调 silence_timeout 到 60s
    # → monolog 阈值 = silence/2 = 30s。t=50 时距上次"多人活跃"(t=24) 26s < 30s，
    # 既不触发 silence_timeout 也不触发 degenerated_to_monolog。
    det = AutoMeetingDetector(silence_timeout_s=60.0, min_active_seconds=6.0)
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    # 20s 后又说话
    det.observe(speaker_id="spk_A", duration_ms=3_000, now=_at(24))
    # 50s 时距上次说话 26s < 60s → 不结束
    evs = det.observe(speaker_id=None, duration_ms=0, now=_at(50))
    assert all(e.kind != "end" for e in evs)
    assert det.active_meeting_id is not None


# ──────────────────────────────────────────────────────────────────────
# 2026-05 phase4-meeting-deadlock：max_duration + degenerated_to_monolog
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_max_duration_force_end() -> None:
    """硬上限：start 之后超过 max_meeting_duration_s 必须 emit end。

    回归 2026-05 「会议中 9h+」bug：持续环境音让 silence_timeout 永远凑不齐，
    detector 没有兜底机制。
    """
    det = AutoMeetingDetector(
        min_active_seconds=6.0,
        silence_timeout_s=30.0,
        # 30 min（与 prod 默认一致；测试方便）
        max_meeting_duration_s=1800.0,
    )
    # 触发会议
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"
    auto_mid = start[0].meeting_id

    # 31 min 后继续喂多人 chunk（注意 last_voice_at 一直在更新，
    # silence_timeout 永远凑不齐 → 唯一能 end 的就是 max_duration_exceeded）
    evs_a = det.observe(speaker_id="spk_A", duration_ms=3_000, now=_at(31 * 60))
    evs_b = det.observe(speaker_id="spk_B", duration_ms=3_000, now=_at(31 * 60 + 2))
    ends = [e for e in evs_a + evs_b if e.kind == "end"]
    assert any(e.reason == "max_duration_exceeded" and e.meeting_id == auto_mid for e in ends)
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_degenerated_to_monolog_ends() -> None:
    """退化为独白：start 后单人持续说话直到 ≥2 人活跃信号在窗口里消失
    超过 silence/2 → emit end。

    这是 max_duration（30 min 兜底）之外更早的一道防线：会议要"≥2 人活跃"
    才会开；一旦"多人活跃"信号从滑动窗口里淡出且持续 silence/2 都没回来，
    就视为"会议已结束，剩下的人在自言自语"。
    """
    det = AutoMeetingDetector(
        # 缩小窗口和 silence，测试不需要等几十秒；
        # monolog 阈值 = silence/2 = 5s
        window_s=10.0,
        min_active_seconds=6.0,
        silence_timeout_s=10.0,
        max_meeting_duration_s=1800.0,
    )
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(2))
    assert start[0].kind == "start"
    auto_mid = start[0].meeting_id

    # 只喂 A：
    #   t=3..12: B@2 仍在 window（cutoff=t-10），distinct=2，_distinct_active_at 每次刷新
    #   t=13: cutoff=3，B@2 出窗口；_distinct_active_at 停留在 12
    #   t > 12 + 5 = 17 才该触发 monolog
    end_event = None
    for t in (3, 6, 9, 12, 13, 14, 16, 18, 20):
        evs = det.observe(speaker_id="spk_A", duration_ms=2_000, now=_at(t))
        ends = [e for e in evs if e.kind == "end" and e.meeting_id == auto_mid]
        if ends:
            end_event = ends[0]
            assert t >= 17, f"monolog 在 t={t} 过早触发（应 ≥ 17）"
            break
    assert end_event is not None, "未在 silence/2 后 emit degenerated_to_monolog"
    assert end_event.reason == "degenerated_to_monolog"
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_silence_timeout_still_works() -> None:
    """回归保护：原 silence_timeout 行为不变（新规则不抢先 end）。"""
    det = AutoMeetingDetector(
        silence_timeout_s=30.0,
        min_active_seconds=6.0,
        max_meeting_duration_s=1800.0,
    )
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"
    auto_mid = start[0].meeting_id

    # 40s 完全静默（既没多人活跃也没单人活跃），应走 silence_timeout
    evs = det.observe(speaker_id=None, duration_ms=0, now=_at(40))
    ends = [e for e in evs if e.kind == "end" and e.meeting_id == auto_mid]
    assert ends, "silence_timeout 未触发 end"
    assert ends[0].reason == "silence_timeout"
    assert det.active_meeting_id is None


@pytest.mark.unit
def test_manual_meeting_priority_unchanged() -> None:
    """回归保护：manual_meeting_id 让步语义不变（不被新规则改写）。"""
    det = AutoMeetingDetector(
        min_active_seconds=6.0,
        silence_timeout_s=30.0,
        max_meeting_duration_s=1800.0,
    )
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"
    auto_mid = start[0].meeting_id

    # 用户手动开始；detector 必须 emit end(reason=manual_meeting_started)
    evs = det.observe(
        speaker_id="spk_A",
        duration_ms=3_000,
        now=_at(10),
        manual_meeting_id="user-mtg-1",
    )
    ends = [e for e in evs if e.kind == "end" and e.meeting_id == auto_mid]
    assert ends and ends[0].reason == "manual_meeting_started"
    assert det.active_meeting_id is None

    # 后续 manual 期间不该再有新 auto 触发，也不该有 max_duration 误报
    evs2 = det.observe(
        speaker_id="spk_C",
        duration_ms=3_000,
        now=_at(20),
        manual_meeting_id="user-mtg-1",
    )
    assert evs2 == []


@pytest.mark.unit
def test_continuous_multispeaker_does_not_end() -> None:
    """≥2 distinct speakers 持续喂 chunk → 既不 silence、也不 monolog（max 之内）。"""
    det = AutoMeetingDetector(
        window_s=30.0,
        min_active_seconds=6.0,
        silence_timeout_s=30.0,
        max_meeting_duration_s=1800.0,
    )
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    start = det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    assert start[0].kind == "start"

    # 模拟 20 min 内交替说话，每次 chunk 距离 5s。Detector 应保持 in-meeting。
    saw_unexpected_end = False
    for i in range(1, 240):
        t = 4 + i * 5
        if t >= 1800:  # 接近 max_duration，不在本测试覆盖
            break
        spk = "spk_A" if i % 2 == 0 else "spk_B"
        evs = det.observe(speaker_id=spk, duration_ms=3_000, now=_at(t))
        if any(e.kind == "end" for e in evs):
            saw_unexpected_end = True
            break
    assert not saw_unexpected_end, "多人持续说话被误判为 end"
    assert det.active_meeting_id is not None
