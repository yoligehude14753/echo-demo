"""自动会议检测状态机：根据 ambient 主链路里的说话人活动自动 start / end meeting。

触发规则（保守为先，避免误开会）：
- 滑动窗口（默认 30s）内 distinct speakers ≥ 2
- 窗口内总语音 duration ≥ ``min_active_seconds``（默认 6s）
- 如果声纹暂时识别不出 speaker_id，但 STT 已连续给出有效语音，走更保守的
  ``unknown_speaker_min_active_seconds`` fallback 自动开始记录
- 当前没在 meeting 中（手动 @开始 优先；用户手动开会时 detector 自动让步）
- 触发后进入 ``auto_meeting`` 状态，meeting_id = ``auto-<unix_ts>``

结束规则（任意一条命中即 end auto-meeting）：
- 静默 ≥ ``silence_timeout_s``（默认 30s） → reason="silence_timeout"
- 距 start 超过 ``max_meeting_duration_s``（默认 30 min 硬上限）
    → reason="max_duration_exceeded"
    （兜底：避免持续环境音 / 单人独白 / 电视背景音让会议永远不结束。
     2026-05 echodesk 顶栏「会议中 562:53」9h+ bug 的结构性修复。）
- 退化为独白：窗口内 distinct speakers ≤ 1 且距上次有 ≥2 人活跃
  超过 ``silence_timeout_s / 2`` → reason="degenerated_to_monolog"
    （会议本来就要"≥2 人"才会自动开；一旦长时间只剩一个人说话，
     就算不静默也应该回到 idle，让 ambient 链路继续归档，
     而不是让顶栏一直亮"会议中"。）

冷却：
- cooldown：刚 end 后 ``cooldown_s``（默认 60s）内不再触发，避免抖动

与手动 @开始会议 合并策略：
- 手动 meeting_id 传入 observe()，detector 会立即取消自己当前的 auto meeting（end 事件）
- detector 之后停止统计直到手动结束（manual_meeting_id 重新为 None）

不直接依赖 MeetingPipeline / EventBus：只产出 ``DetectorEvent``，由调用者执行副作用。
这保持了纯函数式的可测性。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger("echodesk.auto_meeting")

DetectorEventKind = Literal["start", "end"]


class DetectorEvent(BaseModel):
    kind: DetectorEventKind
    meeting_id: str
    reason: str = ""


class AutoMeetingDetector:
    def __init__(
        self,
        *,
        window_s: float = 30.0,
        min_distinct_speakers: int = 2,
        min_active_seconds: float = 6.0,
        unknown_speaker_min_active_seconds: float | None = None,
        silence_timeout_s: float = 30.0,
        cooldown_s: float = 60.0,
        max_meeting_duration_s: float = 1800.0,
    ) -> None:
        self._window_s = window_s
        self._min_distinct = min_distinct_speakers
        self._min_active = min_active_seconds
        self._unknown_min_active = (
            unknown_speaker_min_active_seconds
            if unknown_speaker_min_active_seconds is not None
            else max(min_active_seconds * 1.5, 10.0)
        )
        self._silence = silence_timeout_s
        self._cooldown = cooldown_s
        self._max_meeting_duration_s = max_meeting_duration_s

        self._window: list[tuple[datetime, str, int]] = []  # (t, speaker_id, dur_ms)
        self._unknown_voice_window: list[tuple[datetime, int]] = []  # (t, dur_ms)
        self._active_meeting_id: str | None = None
        self._last_voice_at: datetime | None = None
        self._last_end_at: datetime | None = None
        # 会议开始时间：用于 max_meeting_duration_s 兜底
        self._meeting_started_at: datetime | None = None
        # 上次"窗口内 distinct ≥ min_distinct"的时刻：用于检测 degenerated_to_monolog
        self._distinct_active_at: datetime | None = None

    @property
    def active_meeting_id(self) -> str | None:
        return self._active_meeting_id

    def observe(
        self,
        *,
        speaker_id: str | None,
        duration_ms: int,
        now: datetime,
        manual_meeting_id: str | None = None,
    ) -> list[DetectorEvent]:
        """处理一次 ambient chunk 观测，可能 emit 0..2 个 detector events。

        参数：
        - speaker_id: 该 chunk 识别出的说话人；可为 None（静默或未识别）
        - duration_ms: 该 chunk 语音活动时长（用 STT end_ms 估算）
        - now: chunk 的 wall-clock 时间
        - manual_meeting_id: 上层显式传入的会议 id（手动 @开始）；非 None 时 detector 让步
        """
        out: list[DetectorEvent] = []

        # 1. 手动会议优先 → 让步并取消 auto
        if manual_meeting_id is not None:
            if self._active_meeting_id is not None:
                out.append(
                    DetectorEvent(
                        kind="end",
                        meeting_id=self._active_meeting_id,
                        reason="manual_meeting_started",
                    )
                )
                self._clear_active(now)
            if speaker_id:
                self._last_voice_at = now
            return out

        # 2. 维护窗口
        self._prune_window(now)
        if duration_ms > 0:
            self._last_voice_at = now
            if speaker_id:
                self._window.append((now, speaker_id, duration_ms))
            else:
                self._unknown_voice_window.append((now, duration_ms))

        # 窗口内 distinct ≥ min_distinct → 刷新"多人活跃时刻"（用于 monolog 退化判定）
        distinct_now = {s for (_, s, _) in self._window}
        if len(distinct_now) >= self._min_distinct:
            self._distinct_active_at = now

        # 3. 已在 auto_meeting：检查三类 end 触发
        if self._active_meeting_id is not None:
            ended = self._maybe_end_active(now, out)
            if ended:
                return out
            return out

        # 4. idle 状态：检查触发条件
        if self._in_cooldown(now):
            return out

        active_ms = sum(d for (_, _, d) in self._window)
        unknown_active_ms = sum(d for (_, d) in self._unknown_voice_window)
        if len(distinct_now) >= self._min_distinct and active_ms >= self._min_active * 1000:
            new_id = f"auto-{int(now.timestamp())}"
            self._active_meeting_id = new_id
            self._meeting_started_at = now
            self._distinct_active_at = now
            out.append(
                DetectorEvent(
                    kind="start",
                    meeting_id=new_id,
                    reason=f"distinct_speakers={len(distinct_now)} active_ms={active_ms}",
                )
            )
        elif unknown_active_ms >= self._unknown_min_active * 1000:
            new_id = f"auto-{int(now.timestamp())}"
            self._active_meeting_id = new_id
            self._meeting_started_at = now
            out.append(
                DetectorEvent(
                    kind="start",
                    meeting_id=new_id,
                    reason=f"unknown_speaker_active_ms={unknown_active_ms}",
                )
            )
        return out

    def force_end(self, *, now: datetime, reason: str = "external") -> DetectorEvent | None:
        """外部强制结束（例如 ambient 端检测到长时间无任何 chunk）。"""
        if self._active_meeting_id is None:
            return None
        ev = DetectorEvent(kind="end", meeting_id=self._active_meeting_id, reason=reason)
        self._clear_active(now)
        return ev

    def reset(self) -> None:
        self._window.clear()
        self._unknown_voice_window.clear()
        self._active_meeting_id = None
        self._last_voice_at = None
        self._last_end_at = None
        self._meeting_started_at = None
        self._distinct_active_at = None

    def _clear_active(self, now: datetime) -> None:
        """end auto meeting 时统一清状态（保留 _last_end_at 进 cooldown）。"""
        self._active_meeting_id = None
        self._last_end_at = now
        self._meeting_started_at = None
        self._distinct_active_at = None

    def _maybe_end_active(self, now: datetime, out: list[DetectorEvent]) -> bool:
        """三类 end 触发判定；按优先级依次检查，命中即返回 True 并 append 事件。

        优先级：max_duration > silence_timeout > degenerated_to_monolog
        （max_duration 是兜底硬上限，需要最先生效；silence 是用户最直观的"散会"语义；
         monolog 是补充规则，触发频率较低）
        """
        assert self._active_meeting_id is not None

        # 3.1 硬上限（防止任意原因导致会议永不结束的兜底）
        if (
            self._meeting_started_at is not None
            and (now - self._meeting_started_at).total_seconds() > self._max_meeting_duration_s
        ):
            out.append(
                DetectorEvent(
                    kind="end",
                    meeting_id=self._active_meeting_id,
                    reason="max_duration_exceeded",
                )
            )
            self._clear_active(now)
            return True

        # 3.2 静默超时
        if self._last_voice_at is not None and (
            (now - self._last_voice_at).total_seconds() > self._silence
        ):
            out.append(
                DetectorEvent(
                    kind="end",
                    meeting_id=self._active_meeting_id,
                    reason="silence_timeout",
                )
            )
            self._clear_active(now)
            return True

        # 3.3 退化为独白（窗口内 ≤ 1 个 speaker，且距上次"多人活跃"已超过 silence/2）
        distinct = {s for (_, s, _) in self._window}
        if (
            len(distinct) <= 1
            and self._distinct_active_at is not None
            and (now - self._distinct_active_at).total_seconds() > self._silence / 2
        ):
            out.append(
                DetectorEvent(
                    kind="end",
                    meeting_id=self._active_meeting_id,
                    reason="degenerated_to_monolog",
                )
            )
            self._clear_active(now)
            return True

        return False

    def _prune_window(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_s)
        self._window = [(t, s, d) for (t, s, d) in self._window if t >= cutoff]
        self._unknown_voice_window = [
            (t, d) for (t, d) in self._unknown_voice_window if t >= cutoff
        ]

    def _in_cooldown(self, now: datetime) -> bool:
        if self._last_end_at is None:
            return False
        return (now - self._last_end_at).total_seconds() < self._cooldown


__all__ = ["AutoMeetingDetector", "DetectorEvent"]
