"""全局会议状态机（单例 idle/in_meeting）。

设计意图（修复 echo-demo 之前 auto-meeting 爆炸的 bug）：
- 全局只能有 0 或 1 个会议；UI 状态栏点击切换；auto detector 触发自动开/结。
- 这里是**唯一**调用 MeetingPipeline.start/end 的入口，detector 不再直接调用 pipeline。
- 跨重启时从 repo 恢复唯一的 in_meeting 会议（多于 1 个时取最新的，其余强制 end）。

状态转换：
- idle → in_meeting(auto)：detector emit start_event
- idle → in_meeting(manual)：用户点击状态栏
- in_meeting(auto) → idle：detector emit end_event (silence_timeout) → finalize 纪要
- in_meeting(manual) → idle：用户点击状态栏 → finalize 纪要
- in_meeting(auto) → in_meeting(manual)：用户在自动会议中点击"接管"
- 自动检测时遇到已有 manual 会议：detector 让步（manual_meeting_id 进 observe）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.ports.event_bus import EventBusPort
from app.ports.repository import RepositoryPort
from app.schemas.events import EchoEvent
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline

logger = logging.getLogger("echodesk.meeting_state")

MeetingMode = Literal["idle", "in_meeting"]
StartReason = Literal["auto", "manual"]


@dataclass(slots=True)
class CurrentMeeting:
    meeting_id: str
    started_at: datetime
    started_by: StartReason  # auto | manual


class MeetingState:
    def __init__(
        self,
        *,
        pipeline: MeetingPipeline,
        detector: AutoMeetingDetector,
        repository: RepositoryPort | None = None,
        event_bus: EventBusPort | None = None,
        max_meeting_duration_s: float = 1800.0,
    ) -> None:
        self._pipeline = pipeline
        self._detector = detector
        self._repo = repository
        self._event_bus = event_bus
        self._max_meeting_duration_s = max_meeting_duration_s
        self._current: CurrentMeeting | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> CurrentMeeting | None:
        return self._current

    @property
    def mode(self) -> MeetingMode:
        return "in_meeting" if self._current else "idle"

    async def hydrate(self) -> None:
        """启动时恢复唯一的 in_meeting 会议；多于 1 个时强制结束所有旧的。

        旧 bug：detector reset 后新 chunk 又开新 auto-meeting → sqlite 里堆出
        几十个 auto-XXX。修复办法：启动时强制清理 in_meeting > 1 的状态。

        2026-05 新增：若保留下来的"最新会议"是 auto-* 且 started_at 已超过
        ``max_meeting_duration_s``，也强制结束并清空 _current。
        这条规则关掉的结构性 bug 是：detector 进程崩溃 / 进程被杀重启时，
        sqlite 里残留的 auto-* in_meeting 行会让顶栏继续显示"会议中"，
        但 detector 已经丢了内存状态，永远不会再 emit silence_timeout。
        """
        if self._repo is None:
            return
        meetings = await self._repo.list_meetings(state="in_meeting", limit=100)
        if not meetings:
            return
        meetings_sorted = sorted(meetings, key=lambda m: m.started_at, reverse=True)
        keep = meetings_sorted[0]
        for stale in meetings_sorted[1:]:
            try:
                await self._repo.update_meeting_state(
                    stale.id, state="ended", ended_at=datetime.now(UTC)
                )
                logger.warning("hydrate: forced-end stale meeting %s", stale.id)
            except Exception as e:
                logger.warning("hydrate: failed to force-end %s: %s", stale.id, e)

        now = datetime.now(UTC)
        kept_started_at = keep.started_at
        # tz-aware 兜底：sqlite naive datetime 在 macOS Python 上偶尔回来无 tzinfo
        if kept_started_at.tzinfo is None:
            kept_started_at = kept_started_at.replace(tzinfo=UTC)
        age_s = (now - kept_started_at).total_seconds()
        if keep.id.startswith("auto-") and age_s > self._max_meeting_duration_s:
            try:
                await self._repo.update_meeting_state(keep.id, state="ended", ended_at=now)
            except Exception as e:
                logger.warning("hydrate: failed to force-end stale auto-meeting %s: %s", keep.id, e)
            logger.warning(
                "hydrate: stale auto-meeting force-ended %s (age=%.1fs > max=%.1fs)",
                keep.id,
                age_s,
                self._max_meeting_duration_s,
            )
            self._current = None
            return

        self._current = CurrentMeeting(
            meeting_id=keep.id,
            started_at=keep.started_at,
            started_by="auto" if keep.id.startswith("auto-") else "manual",
        )
        logger.info("hydrated current meeting: %s", keep.id)

    # ── 用户手动控制 ────────────────────────────────────────────────

    async def manual_start(self, *, title: str | None = None) -> CurrentMeeting:
        """用户点击状态栏开始会议。已在会议中则原样返回当前会议。"""
        async with self._lock:
            if self._current is not None:
                return self._current
            mid = f"m-{uuid.uuid4().hex[:12]}"
            await self._pipeline.start_meeting(mid, title=title, auto_started=False)
            self._current = CurrentMeeting(
                meeting_id=mid,
                started_at=datetime.now(UTC),
                started_by="manual",
            )
            await self._publish(
                "meeting.state_changed",
                mid,
                {
                    "mode": "in_meeting",
                    "started_by": "manual",
                    "reason": "user_clicked",
                },
            )
            return self._current

    async def manual_end(self) -> str | None:
        """用户点击状态栏结束会议；finalize 纪要并清空状态。"""
        async with self._lock:
            cur = self._current
            if cur is None:
                return None
            self._current = None
        # finalize 放到 lock 外（LLM 调用耗时，避免堵 ambient 链路）
        try:
            await self._pipeline.finalize_meeting(cur.meeting_id, title=cur.meeting_id)
        except Exception as e:
            logger.warning("manual_end finalize failed (still ending): %s", e)
            await self._pipeline.end_meeting(cur.meeting_id)
        # 让 detector 进 cooldown，避免用户结束后立刻又被自动开
        self._detector.force_end(now=datetime.now(UTC), reason="manual_end")
        await self._publish(
            "meeting.state_changed",
            cur.meeting_id,
            {
                "mode": "idle",
                "ended_by": "manual",
            },
        )
        return cur.meeting_id

    # ── ambient 链路调用：每 chunk 喂一次 ───────────────────────────

    async def observe_chunk(
        self,
        *,
        speaker_id: str | None,
        duration_ms: int,
        now: datetime,
    ) -> str | None:
        """ambient 每个 chunk 调一次。

        - 当前 idle：让 detector 判断是否自动 start
        - 当前 in_meeting(auto)：让 detector 判断是否 silence_timeout end
        - 当前 in_meeting(manual)：把 manual_meeting_id 喂进 detector 让其让步
        - 返回 effective_meeting_id，供 ambient pipeline 叠加 meeting overlay
        """
        manual_mid = (
            self._current.meeting_id
            if self._current is not None and self._current.started_by == "manual"
            else None
        )
        events = self._detector.observe(
            speaker_id=speaker_id,
            duration_ms=duration_ms,
            now=now,
            manual_meeting_id=manual_mid,
        )
        for ev in events:
            if ev.kind == "start":
                await self._apply_auto_start(ev.meeting_id, reason=ev.reason)
            elif ev.kind == "end":
                await self._apply_auto_end(ev.meeting_id, reason=ev.reason)
        return self._current.meeting_id if self._current else None

    async def _apply_auto_start(self, meeting_id: str, *, reason: str) -> None:
        async with self._lock:
            if self._current is not None:
                # 已有会议（多半是 manual 进来后 detector 才触发的）→ 忽略 detector start
                logger.debug("auto-start ignored (already in meeting): %s", reason)
                return
            self._current = CurrentMeeting(
                meeting_id=meeting_id,
                started_at=datetime.now(UTC),
                started_by="auto",
            )
        await self._pipeline.start_meeting(meeting_id, auto_started=True)
        await self._publish("meeting.auto_detected", meeting_id, {"reason": reason})
        await self._publish(
            "meeting.state_changed",
            meeting_id,
            {
                "mode": "in_meeting",
                "started_by": "auto",
                "reason": reason,
            },
        )

    async def _apply_auto_end(self, meeting_id: str, *, reason: str) -> None:
        async with self._lock:
            cur = self._current
            if cur is None or cur.meeting_id != meeting_id:
                return
            self._current = None
        try:
            await self._pipeline.finalize_meeting(meeting_id, title=meeting_id)
        except Exception as e:
            logger.warning("auto-end finalize failed: %s; fallback to end_meeting", e)
            await self._pipeline.end_meeting(meeting_id)
        await self._publish("meeting.auto_ended", meeting_id, {"reason": reason})
        await self._publish(
            "meeting.state_changed",
            meeting_id,
            {
                "mode": "idle",
                "ended_by": "auto",
                "reason": reason,
            },
        )

    async def _publish(self, event_type: str, meeting_id: str, payload: dict[str, object]) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                EchoEvent(type=event_type, meeting_id=meeting_id, payload=payload)  # type: ignore[arg-type]
            )
        except Exception as e:
            logger.warning("publish %s failed: %s", event_type, e)


__all__ = ["CurrentMeeting", "MeetingMode", "MeetingState", "StartReason"]
