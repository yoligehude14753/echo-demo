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
    det = AutoMeetingDetector(silence_timeout_s=30.0, min_active_seconds=6.0)
    det.observe(speaker_id="spk_A", duration_ms=4_000, now=_at(0))
    det.observe(speaker_id="spk_B", duration_ms=4_000, now=_at(4))
    # 20s 后又说话
    det.observe(speaker_id="spk_A", duration_ms=3_000, now=_at(24))
    # 50s 时距上次说话 26s < 30s → 不结束
    evs = det.observe(speaker_id=None, duration_ms=0, now=_at(50))
    assert all(e.kind != "end" for e in evs)
    assert det.active_meeting_id is not None
