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
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.ports.event_bus import EventBusPort
from app.ports.repository import MeetingRecord, RepositoryPort
from app.schemas.events import EchoEvent
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline

logger = logging.getLogger("echodesk.meeting_state")

MeetingMode = Literal["idle", "in_meeting"]
StartReason = Literal["auto", "manual"]


def _should_force_end_on_hydrate(meeting_id: str) -> bool:
    """重启恢复时可兜底关闭的会议 id。

    auto-* 是自动会议；m-local-* 是历史桌面端创建的本地会议。
    它们如果跨重启仍停留在 in_meeting，通常已经失去前端控制链路，
    继续保活只会污染会议计数和阻塞状态恢复。
    """
    return meeting_id.startswith("auto-") or meeting_id.startswith("m-local-")


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
        manual_max_meeting_duration_s: float = 4 * 60 * 60,
        manual_inactivity_timeout_s: float = 15 * 60,
        recovery_max_age_s: float = 24 * 60 * 60,
        finalize_callback: Callable[[str, str], Awaitable[object]] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._detector = detector
        self._repo = repository
        self._event_bus = event_bus
        self._max_meeting_duration_s = max_meeting_duration_s
        self._manual_max_meeting_duration_s = manual_max_meeting_duration_s
        self._manual_inactivity_timeout_s = manual_inactivity_timeout_s
        self._recovery_max_age_s = recovery_max_age_s
        self._finalize_callback = finalize_callback
        self._current: CurrentMeeting | None = None
        self._lock = asyncio.Lock()
        self._hydrate_lock = asyncio.Lock()
        self._hydrated = False
        self._watchdog_task: asyncio.Task[None] | None = None
        self._finalize_tasks: set[asyncio.Task[None]] = set()
        self._last_valid_speech_at: datetime | None = None

    async def _finalize(self, meeting_id: str, title: str) -> object:
        if self._finalize_callback is not None:
            return await self._finalize_callback(meeting_id, title)
        return await self._pipeline.finalize_meeting(meeting_id, title=title)

    @property
    def current(self) -> CurrentMeeting | None:
        return self._current

    @property
    def mode(self) -> MeetingMode:
        return "in_meeting" if self._current else "idle"

    def start_watchdog(self, *, interval_s: float = 5.0) -> None:
        """启动会议生命周期 watchdog。

        detector 的 silence/max 检查依赖 ambient chunk 进入 ``observe_chunk``。
        如果麦克风/上传链路断流，或者恢复出历史 ``m-local-*`` 状态，就可能没有
        新 chunk 触发检查。watchdog 作为状态机层兜底：auto meeting 推进
        silence/max 检查；manual meeting 按“无有效语音”优先、4h 硬上限兜底。
        """
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop(interval_s=interval_s))

    async def stop_watchdog(self) -> None:
        """停止会议生命周期 watchdog（FastAPI lifespan shutdown 调用）。"""
        task = self._watchdog_task
        if task is None:
            return
        self._watchdog_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watchdog_loop(self, *, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.hydrate()
                await self._check_lifecycle(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - watchdog must never kill backend
                logger.warning("meeting watchdog tick failed: %s", e)

    async def hydrate(self) -> None:
        """Load the principal's durable active meeting exactly once per runtime.

        Scoped runtimes may be created lazily after process startup or recreated
        after the idle-TTL janitor evicts them. Every async state transition calls
        this gate, so no request can observe a fresh in-memory ``idle`` state
        before the repository has been consulted.
        """

        if self._hydrated:
            return
        async with self._hydrate_lock:
            if self._hydrated:
                return
            await self._hydrate_from_repo()
            self._hydrated = True

    async def _hydrate_from_repo(self) -> None:
        """启动时恢复唯一的 in_meeting 会议；多于 1 个时强制结束所有旧的。

        旧 bug：detector reset 后新 chunk 又开新 auto-meeting → sqlite 里堆出
        几十个 auto-XXX。修复办法：启动时强制清理 in_meeting > 1 的状态。

        2026-05 新增：若保留下来的"最新会议"是 auto-* 且 started_at 已超过
        ``max_meeting_duration_s``，也强制结束并清空 _current。
        这条规则关掉的结构性 bug 是：detector 进程崩溃 / 进程被杀重启时，
        sqlite 里残留的 auto-* in_meeting 行会让顶栏继续显示"会议中"，
        但 detector 已经丢了内存状态，永远不会再 emit silence_timeout。

        2026-07 扩展：同样清理历史桌面端遗留的 m-local-*；所有普通手动会议
        超过 recovery_max_age_s（默认 24h）也视为陈旧。短时崩溃重启仍可续接，
        但跨天状态不能继续让顶栏无限累计。
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
        uses_system_limit = _should_force_end_on_hydrate(keep.id)
        expiry_s = (
            self._max_meeting_duration_s
            if uses_system_limit
            else min(self._manual_max_meeting_duration_s, self._recovery_max_age_s)
        )
        if age_s > expiry_s:
            try:
                await self._repo.update_meeting_state(keep.id, state="ended", ended_at=now)
            except Exception as e:
                logger.warning("hydrate: failed to force-end stale meeting %s: %s", keep.id, e)
                # durable fence 失败时不能向 UI 假装 idle；继续 hydrate 为 current，
                # watchdog 会在下一 tick 重试正常的统一结束路径。
            else:
                logger.warning(
                    "hydrate: stale meeting force-ended %s (age=%.1fs > max=%.1fs)",
                    keep.id,
                    age_s,
                    expiry_s,
                )
                self._current = None
                self._last_valid_speech_at = None
                self._detector.enter_cooldown(now)
                try:
                    if await self._pipeline.load_meeting_for_retry(keep.id):
                        title = _resolve_meeting_title(keep.title, keep.id)
                        self._schedule_finalize(keep.id, title)
                except Exception as e:
                    logger.warning("hydrate: failed to schedule stale finalize %s: %s", keep.id, e)
                return

        # Scoped runtimes are created lazily and may be evicted independently of
        # the process lifespan.  Restoring only the state-machine pointer would
        # leave a fresh MeetingPipeline with no pre-eviction transcript.  The
        # first new chunk would then make the in-memory list non-empty and the
        # finalize path would silently summarize only post-eviction segments.
        # Hydrate the pipeline before exposing the durable meeting as current so
        # every subsequent ingest/finalize sees the complete transcript.
        await self._pipeline.hydrate_from_repo()

        self._current = CurrentMeeting(
            meeting_id=keep.id,
            started_at=keep.started_at,
            started_by="auto" if keep.auto_started else "manual",
        )
        # 持久层没有可靠的“最后有效语音”时间；恢复时从 now 开始给完整宽限，
        # 避免仅因进程停机时间较长而立即误结束仍在继续的会议。
        self._last_valid_speech_at = now
        if self._current.started_by == "auto":
            self._adopt_current_auto_meeting(now)
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
        stuck = [
            m
            for m in meetings
            if (not m.minutes_json)
            and m.minutes_status != "ok"
            and m.minutes_cleared_at is None
            and m.minutes_generation_cancelled_at is None
        ]
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
                await self._finalize(m.id, title)
                logger.info("recover: meeting %s minutes regenerated successfully", m.id)
            except Exception as e:
                # finalize_meeting 内部已经把 minutes_status 置为 generation_failed
                logger.warning("recover: meeting %s finalize retry failed: %s", m.id, e)
        return len(stuck)

    # ── 用户手动控制 ────────────────────────────────────────────────

    async def manual_start(self, *, title: str | None = None) -> CurrentMeeting:
        """用户点击状态栏开始会议。已在会议中则原样返回当前会议。"""
        await self.hydrate()
        async with self._lock:
            if self._current is not None:
                return self._current
            mid = f"m-{uuid.uuid4().hex[:12]}"
            record = await self._pipeline.start_meeting(mid, title=title, auto_started=False)
            authoritative = (
                record
                if isinstance(record, MeetingRecord)
                else MeetingRecord(
                    id=mid,
                    title=title,
                    state="in_meeting",
                    started_at=datetime.now(UTC),
                    auto_started=False,
                )
            )
            self._current = CurrentMeeting(
                meeting_id=authoritative.id,
                started_at=authoritative.started_at,
                started_by="auto" if authoritative.auto_started else "manual",
            )
            self._last_valid_speech_at = datetime.now(UTC)
            await self._publish(
                "meeting.state_changed",
                authoritative.id,
                {
                    "mode": "in_meeting",
                    "started_by": self._current.started_by,
                    "reason": "user_clicked",
                },
            )
            return self._current

    async def manual_end(self) -> str | None:
        """先提交会议结束并返回 idle，纪要在后台生成。"""
        await self.hydrate()
        cur = self._current
        if cur is None:
            return None
        ended_at = datetime.now(UTC)
        ended = await self._end_current(
            cur.meeting_id,
            ended_at=ended_at,
            ended_by="manual",
            reason="manual_end",
        )
        return cur.meeting_id if ended else None

    async def end_without_finalize(
        self,
        meeting_id: str,
        *,
        reason: str = "low_level_api_end",
    ) -> bool:
        """结束 low-level meeting overlay，并同步全局 idle/cooldown。

        ``POST /meetings/{id}/end`` 的历史契约是不生成纪要（调用方会显式
        ``/finalize``），所以这里不能复用会调度后台纪要的 ``_end_current``。
        但它仍必须清掉匹配的 ``current`` 并写 detector cooldown，否则同一环境音
        可以在结束后马上重新触发 auto meeting。

        返回值表示该 meeting 是否也是当前全局会议。
        """

        await self.hydrate()
        ended_at = datetime.now(UTC)
        async with self._lock:
            current_matches = (
                self._current is not None and self._current.meeting_id == meeting_id
            )
            await self._pipeline.end_meeting(meeting_id, ended_at=ended_at)
            if current_matches:
                self._current = None
                self._last_valid_speech_at = None

        self._detector.enter_cooldown(ended_at)
        if current_matches:
            await self._publish(
                "meeting.state_changed",
                meeting_id,
                {
                    "mode": "idle",
                    "ended_by": "manual",
                    "reason": reason,
                },
            )
        return current_matches

    async def _end_current(
        self,
        meeting_id: str,
        *,
        ended_at: datetime,
        ended_by: str,
        reason: str,
    ) -> bool:
        """统一结束入口：durable ended → idle/cooldown → 后台 finalize。"""

        title = await self._resolve_title(meeting_id)
        async with self._lock:
            cur = self._current
            if cur is None or cur.meeting_id != meeting_id:
                return False
            # 这里只做本地/SQLite fence，不触发 LLM，通常可在一个请求往返内完成。
            await self._pipeline.end_meeting(meeting_id, ended_at=ended_at)
            self._current = None
            self._last_valid_speech_at = None

        # 所有结束来源都必须写 cooldown；manual end 时 detector 没 active id，
        # 不能再依赖 force_end 的“有 active 才生效”语义。
        self._detector.enter_cooldown(ended_at)
        if ended_by == "auto":
            await self._publish("meeting.auto_ended", meeting_id, {"reason": reason})
        await self._publish(
            "meeting.state_changed",
            meeting_id,
            {
                "mode": "idle",
                "ended_by": ended_by,
                "reason": reason,
            },
        )
        self._schedule_finalize(meeting_id, title)
        return True

    def _schedule_finalize(self, meeting_id: str, title: str) -> None:
        """持有 fire-and-forget 任务，避免请求等待 LLM/产物投影。"""

        task = asyncio.create_task(
            self._finalize_in_background(meeting_id, title),
            name=f"meeting-finalize-{meeting_id}",
        )
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    async def _finalize_in_background(self, meeting_id: str, title: str) -> None:
        try:
            await self._finalize(meeting_id, title)
        except Exception as e:
            # pipeline/workflow 会持久化 generation_failed；若在 dispatch 前失败，
            # startup recover_stuck_minutes 仍会从 ended + 空纪要恢复。
            logger.warning("background finalize failed for %s: %s", meeting_id, e)

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
        is_valid_speech: bool | None = None,
    ) -> str | None:
        """ambient 每个 chunk 调一次。

        ``duration_ms`` 只代表 VAD 判定的有效语音时长，不得使用 STT 请求耗时。
        ``is_valid_speech=False`` 时即使 ASR 返回了文本也不会刷新会议活跃时间。

        - 当前 idle：让 detector 判断是否自动 start
        - 当前 in_meeting(auto)：让 detector 判断是否 silence_timeout end
        - 当前 in_meeting(manual)：把 manual_meeting_id 喂进 detector 让其让步
        - 返回 effective_meeting_id，供 ambient pipeline 叠加 meeting overlay
        """
        await self.hydrate()
        if await self._check_lifecycle(now, tick_auto=False):
            return None

        valid_speech = duration_ms > 0 if is_valid_speech is None else is_valid_speech
        if valid_speech and duration_ms > 0 and self._current is not None:
            self._last_valid_speech_at = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

        manual_mid = (
            self._current.meeting_id
            if self._current is not None and self._current.started_by == "manual"
            else None
        )
        events = self._detector.observe(
            speaker_id=speaker_id if valid_speech else None,
            duration_ms=duration_ms if valid_speech else 0,
            now=now,
            manual_meeting_id=manual_mid,
        )
        for ev in events:
            if ev.kind == "start":
                await self._apply_auto_start(ev.meeting_id, reason=ev.reason)
            elif ev.kind == "end":
                await self._apply_auto_end(ev.meeting_id, reason=ev.reason)
        return self._current.meeting_id if self._current else None

    async def note_valid_speech(
        self,
        meeting_id: str,
        *,
        now: datetime,
        is_valid_speech: bool,
    ) -> None:
        """显式 meeting_id 上传路径只更新匹配会议的有效语音心跳。"""

        await self.hydrate()
        if await self._check_lifecycle(now, tick_auto=False):
            return
        if not is_valid_speech:
            return
        cur = self._current
        if cur is not None and cur.meeting_id == meeting_id:
            self._last_valid_speech_at = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    async def _check_lifecycle(self, now: datetime, *, tick_auto: bool = True) -> bool:
        """推进 hard-limit、断流 silence 和 manual inactivity；命中时返回 True。"""

        if await self._meeting_exceeded_max_duration(now):
            return True
        cur = self._current
        if cur is None:
            return False

        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if cur.started_by == "auto":
            self._adopt_current_auto_meeting(now_aware)
            if not tick_auto:
                return False
            for event in self._detector.tick(now=now_aware):
                if event.kind == "end":
                    await self._apply_auto_end(event.meeting_id, reason=event.reason)
                    return True
            return False

        last_valid = self._last_valid_speech_at or cur.started_at
        if last_valid.tzinfo is None:
            last_valid = last_valid.replace(tzinfo=UTC)
        inactive_s = (now_aware - last_valid).total_seconds()
        if inactive_s <= self._manual_inactivity_timeout_s:
            return False
        logger.info(
            "manual meeting has no valid speech: %s inactive=%.1fs timeout=%.1fs",
            cur.meeting_id,
            inactive_s,
            self._manual_inactivity_timeout_s,
        )
        return await self._end_current(
            cur.meeting_id,
            ended_at=now_aware,
            ended_by="system",
            reason="no_valid_speech_timeout",
        )

    async def _meeting_exceeded_max_duration(self, now: datetime) -> bool:
        """运行中会议硬上限兜底，manual 使用独立的约 4h 上限。

        ``AutoMeetingDetector`` 本身也有 max-duration，但 backend 重启后 detector
        的内存状态可能丢失；如果没有新 chunk 进入 detector，会议也可能不再触发
        silence/max 检查。这里同时给 manual meeting 提供最终安全兜底。
        """
        cur = self._current
        if cur is None:
            return False
        started_at = cur.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        age_s = (now_aware - started_at).total_seconds()
        uses_system_limit = cur.started_by == "auto" or _should_force_end_on_hydrate(
            cur.meeting_id
        )
        max_duration_s = (
            self._max_meeting_duration_s
            if uses_system_limit
            else self._manual_max_meeting_duration_s
        )
        if age_s <= max_duration_s:
            return False

        logger.warning(
            "meeting max duration exceeded: %s age=%.1fs max=%.1fs; auto-ending",
            cur.meeting_id,
            age_s,
            max_duration_s,
        )
        return await self._end_current(
            cur.meeting_id,
            ended_at=now_aware,
            ended_by="auto" if cur.started_by == "auto" else "system",
            reason="max_duration_exceeded",
        )

    def _adopt_current_auto_meeting(self, now: datetime) -> None:
        """确保 detector 的 active id 与 MeetingState 的 current auto id 一致。"""
        cur = self._current
        if cur is None or cur.started_by != "auto":
            return
        if self._detector.active_meeting_id == cur.meeting_id:
            return
        started_at = cur.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        adopt_active = getattr(self._detector, "adopt_active", None)
        if callable(adopt_active):
            adopt_active(cur.meeting_id, started_at=started_at, now=now_aware)
            logger.warning("detector adopted current auto meeting: %s", cur.meeting_id)

    async def _apply_auto_start(self, meeting_id: str, *, reason: str) -> None:
        async with self._lock:
            if self._current is not None:
                # 已有会议（多半是 manual 进来后 detector 才触发的）→ 忽略 detector start
                logger.debug("auto-start ignored (already in meeting): %s", reason)
                return
            record = await self._pipeline.start_meeting(meeting_id, auto_started=True)
            authoritative = (
                record
                if isinstance(record, MeetingRecord)
                else MeetingRecord(
                    id=meeting_id,
                    state="in_meeting",
                    started_at=datetime.now(UTC),
                    auto_started=True,
                )
            )
            self._current = CurrentMeeting(
                meeting_id=authoritative.id,
                started_at=authoritative.started_at,
                started_by="auto" if authoritative.auto_started else "manual",
            )
            self._last_valid_speech_at = datetime.now(UTC)
            current = self._current
        if current.started_by == "auto":
            self._adopt_current_auto_meeting(datetime.now(UTC))
            await self._publish("meeting.auto_detected", current.meeting_id, {"reason": reason})
        else:
            # A different process won with a manual meeting while this detector
            # was deciding to auto-start.  Adopt it without emitting a false
            # auto-detected event; the next detector observation yields to it.
            logger.info("auto-start adopted concurrent manual meeting %s", current.meeting_id)
        await self._publish(
            "meeting.state_changed",
            current.meeting_id,
            {
                "mode": "in_meeting",
                "started_by": current.started_by,
                "reason": reason if current.started_by == "auto" else "concurrent_active_meeting",
            },
        )

    async def _apply_auto_end(self, meeting_id: str, *, reason: str) -> None:
        await self._end_current(
            meeting_id,
            ended_at=datetime.now(UTC),
            ended_by="auto",
            reason=reason,
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
