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
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.ports.event_bus import EventBusPort
from app.ports.repository import RepositoryPort
from app.schemas.events import EchoEvent
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline

logger = logging.getLogger("echodesk.meeting_state")

MeetingMode = Literal["idle", "in_meeting"]
StartReason = Literal["auto", "manual"]


def _resolve_meeting_title(stored_title: str | None, meeting_id: str) -> str:
    """给 LLM 纪要选一个合适的 title。

    - 优先用 user 在 manual_start 时给的 title（已经落到 ``meetings.title``）
    - repo 没记 → 用 ``"会议 <id>"``（比直接 ``m-xxxxxx`` 可读）
    - 都没有 → meeting_id 兜底（不可能为空，但防御性）
    """
    if stored_title and stored_title.strip():
        return stored_title.strip()
    if meeting_id:
        return f"会议 {meeting_id}"
    return "未命名会议"


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
        backfill_window_s: float = 90.0,
    ) -> None:
        self._pipeline = pipeline
        self._detector = detector
        self._repo = repository
        self._event_bus = event_bus
        self._max_meeting_duration_s = max_meeting_duration_s
        # 用户 2026-05-28 反馈：「自动识别会议开始要往前覆盖前面的连续对话」。
        # detector 触发时刻晚于真实会议开始 6-30s，本字段控制回溯窗口大小。
        self._backfill_window_s = backfill_window_s
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

    async def recover_stuck_minutes(self) -> int:
        """救活上一次 backend 进程里 finalize 失败、卡住「已结束 · 纪要空」的会议。

        触发条件（任一）：
        - ``state="ended"`` 且 ``minutes_json IS NULL``（旧版本卡住路径，无 status 字段）
        - ``state="ended"`` 且 ``minutes_status="generation_failed"`` 且过 > 1 min 仍未被 UI 重试

        操作：把 segments 重新装回 pipeline 内存，然后调一次 ``finalize_meeting``；
        - 成功 → minutes_status="ok"，发 ``minutes.ready``
        - 失败 → ``_mark_minutes_failed`` 已把 status 维持在 generation_failed，下次再 retry

        本函数 fire-and-forget 跑（不阻塞 startup），返回尝试的会议数。
        """
        if self._repo is None:
            return 0
        meetings = await self._repo.list_meetings(state="ended", limit=20)
        stuck = [m for m in meetings if (not m.minutes_json) and m.minutes_status != "ok"]
        if not stuck:
            return 0
        logger.info("hydrate: %d stuck meeting(s) detected, retrying finalize", len(stuck))
        for m in stuck:
            try:
                ok = await self._pipeline.load_meeting_for_retry(m.id)
            except Exception as e:  # pragma: no cover - 重试前期失败
                logger.warning("recover: load_meeting_for_retry(%s) failed: %s", m.id, e)
                continue
            if not ok:
                logger.warning("recover: meeting %s has no segments to summarize; skip", m.id)
                continue
            title = _resolve_meeting_title(m.title, m.id)
            try:
                await self._pipeline.finalize_meeting(m.id, title=title)
                logger.info("recover: meeting %s minutes regenerated successfully", m.id)
            except Exception as e:
                # finalize_meeting 内部已经把 minutes_status 置为 generation_failed
                logger.warning("recover: meeting %s finalize retry failed: %s", m.id, e)
        return len(stuck)

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
        """用户点击状态栏结束会议；finalize 纪要并清空状态。

        关键修复（2026-05-28，echo-demo backend.log 报错根因）：
        - 之前传 ``title=cur.meeting_id`` 看似没问题，但用户启动会议时给的 title
          会被覆盖成 meeting_id，纪要标题就变成 "m-bdd1da4e7e21" 这种鬼东西。
        - 改为：优先用 repo 里 ``meetings.title``（``manual_start`` 时落库），
          回退 ``"会议 <id>"``。
        - finalize 失败时**不再**调 ``end_meeting``（那个会用空 minutes 把 state
          置 ended 但没有错误状态）。直接让 ``finalize_meeting`` 内部把状态置成
          ``state=ended`` + ``minutes_status="generation_failed"``，UI 据此给「重试」入口。
        """
        async with self._lock:
            cur = self._current
            if cur is None:
                return None
            self._current = None
        title = await self._resolve_title(cur.meeting_id)
        # finalize 放到 lock 外（LLM 调用耗时，避免堵 ambient 链路）
        try:
            await self._pipeline.finalize_meeting(cur.meeting_id, title=title)
        except Exception as e:
            # pipeline 内部已经把 state/minutes_status 落到 ended/generation_failed
            logger.warning("manual_end finalize failed for %s: %s", cur.meeting_id, e)
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

    async def _resolve_title(self, meeting_id: str) -> str:
        """优先取 repo 里 user 启动时落库的 title；缺则回退 ``"会议 <id>"``。

        meeting_id（``m-bdd1da4e7e21`` 之流）作为 LLM 纪要 title 用户体验极差，
        所以只在 repo 完全查不到记录时用作最后兜底。
        """
        if self._repo is not None:
            try:
                rec = await self._repo.get_meeting(meeting_id)
                if rec is not None and rec.title:
                    return rec.title
            except Exception as e:  # pragma: no cover - repo 查询异常
                logger.warning("resolve_title: repo lookup failed for %s: %s", meeting_id, e)
        return _resolve_meeting_title(None, meeting_id)

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
        now = datetime.now(UTC)
        # 用户 2026-05-28 反馈：「自动识别会议开始要往前覆盖前面的连续对话」。
        # detector 累计 6s+ 语音才触发，触发时点已经晚于真实开会 6-30s，
        # 这段时间的 ambient_segments 没有进 meeting，UI 看不到开头。
        # 修复策略："宁可覆盖大不要覆盖小" → 把过去 backfill_window_s 内
        # 的 ambient 整段倒灌进 meeting（去重交给 pipeline.backfill_from_ambient）。
        backfill_since = now - timedelta(seconds=self._backfill_window_s)
        async with self._lock:
            if self._current is not None:
                logger.debug("auto-start ignored (already in meeting): %s", reason)
                return
            self._current = CurrentMeeting(
                meeting_id=meeting_id,
                started_at=backfill_since,
                started_by="auto",
            )
        await self._pipeline.start_meeting(meeting_id, auto_started=True, started_at=backfill_since)
        backfilled = 0
        try:
            backfilled = await self._pipeline.backfill_from_ambient(
                meeting_id, since=backfill_since, until=now
            )
        except Exception as e:
            # backfill 是兜底加分项，失败不影响主流程 / 会议正常进行
            logger.warning("auto-start backfill failed for %s: %s", meeting_id, e)
        logger.info(
            "auto-start backfill: meeting=%s window_s=%.0f segments=%d reason=%s",
            meeting_id,
            self._backfill_window_s,
            backfilled,
            reason,
        )
        await self._publish(
            "meeting.auto_detected",
            meeting_id,
            {
                "reason": reason,
                "backfilled_segments": backfilled,
                "backfill_window_s": self._backfill_window_s,
            },
        )
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
        title = await self._resolve_title(meeting_id)
        try:
            await self._pipeline.finalize_meeting(meeting_id, title=title)
        except Exception as e:
            # pipeline 内部已经标记 minutes_status="generation_failed"，UI 给重试入口
            logger.warning("auto-end finalize failed for %s: %s", meeting_id, e)
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
