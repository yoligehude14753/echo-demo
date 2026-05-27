"""自动会议检测状态机：根据 ambient 主链路里的说话人活动自动 start / end meeting。

触发规则（保守为先，避免误开会）：
- 滑动窗口（默认 30s）内 distinct speakers ≥ 2
- 窗口内总语音 duration ≥ ``min_active_seconds``（默认 6s）
- 当前没在 meeting 中（手动 @开始 优先；用户手动开会时 detector 自动让步）
- 触发后进入 ``auto_meeting`` 状态，meeting_id = ``auto-<unix_ts>``

结束规则：
- 静默 ≥ ``silence_timeout_s``（默认 30s）→ 自动 end
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
        silence_timeout_s: float = 30.0,
        cooldown_s: float = 60.0,
    ) -> None:
        self._window_s = window_s
        self._min_distinct = min_distinct_speakers
        self._min_active = min_active_seconds
        self._silence = silence_timeout_s
        self._cooldown = cooldown_s

        self._window: list[tuple[datetime, str, int]] = []  # (t, speaker_id, dur_ms)
        self._active_meeting_id: str | None = None
        self._last_voice_at: datetime | None = None
        self._last_end_at: datetime | None = None

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
                self._active_meeting_id = None
                self._last_end_at = now
            if speaker_id:
                self._last_voice_at = now
            return out

        # 2. 维护窗口
        self._prune_window(now)
        if speaker_id and duration_ms > 0:
            self._window.append((now, speaker_id, duration_ms))
            self._last_voice_at = now

        # 3. 已在 auto_meeting：检查静默 → end
        if self._active_meeting_id is not None:
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
                self._active_meeting_id = None
                self._last_end_at = now
            return out

        # 4. idle 状态：检查触发条件
        if self._in_cooldown(now):
            return out

        distinct = {s for (_, s, _) in self._window}
        active_ms = sum(d for (_, _, d) in self._window)
        if len(distinct) >= self._min_distinct and active_ms >= self._min_active * 1000:
            new_id = f"auto-{int(now.timestamp())}"
            self._active_meeting_id = new_id
            out.append(
                DetectorEvent(
                    kind="start",
                    meeting_id=new_id,
                    reason=f"distinct_speakers={len(distinct)} active_ms={active_ms}",
                )
            )
        return out

    def force_end(self, *, now: datetime, reason: str = "external") -> DetectorEvent | None:
        """外部强制结束（例如 ambient 端检测到长时间无任何 chunk）。"""
        if self._active_meeting_id is None:
            return None
        ev = DetectorEvent(kind="end", meeting_id=self._active_meeting_id, reason=reason)
        self._active_meeting_id = None
        self._last_end_at = now
        return ev

    def reset(self) -> None:
        self._window.clear()
        self._active_meeting_id = None
        self._last_voice_at = None
        self._last_end_at = None

    def _prune_window(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_s)
        self._window = [(t, s, d) for (t, s, d) in self._window if t >= cutoff]

    def _in_cooldown(self, now: datetime) -> bool:
        if self._last_end_at is None:
            return False
        return (now - self._last_end_at).total_seconds() < self._cooldown


__all__ = ["AutoMeetingDetector", "DetectorEvent"]
