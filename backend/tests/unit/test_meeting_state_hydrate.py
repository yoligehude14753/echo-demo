"""MeetingState.hydrate 单测：跨重启状态恢复 + 过期 auto-meeting 强制结束。

回归 2026-05 phase4-meeting-deadlock：
detector 进程崩溃 / 进程被杀重启时，sqlite 里残留的 auto-* in_meeting 行
会让顶栏继续显示"会议中 9h+"，但 detector 已经丢了内存状态、永远不会再
emit silence_timeout。修复办法：hydrate 时如果保留的最新会议是 auto-* 且
started_at 超过 max_meeting_duration_s，就强制 force-end 并把 _current=None。

手动会议不受此影响（用户能主动看到顶栏并点击结束）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_state import MeetingState


class _PipelineStub:
    """hydrate() 不会调到 pipeline；只需类型占位。"""

    async def start_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def end_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def finalize_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None


class _FinalizingPipelineStub(_PipelineStub):
    def __init__(self, repo: SQLiteRepository) -> None:
        self.repo = repo
        self.finalized: list[tuple[str, str]] = []

    async def finalize_meeting(self, meeting_id: str, *, title: str) -> None:
        self.finalized.append((meeting_id, title))
        await self.repo.update_meeting_state(
            meeting_id,
            state="finalized",
            finalized_at=datetime.now(UTC),
            minutes_json=json.dumps(
                {"meeting_id": meeting_id, "summary": "ok", "sections": []}
            ),
            minutes_status="ok",
            minutes_error="",
        )


async def _make_repo(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    return repo


def _make_state(
    repo: SQLiteRepository,
    *,
    max_meeting_duration_s: float = 1800.0,
    pipeline: Any | None = None,
) -> MeetingState:
    return MeetingState(
        pipeline=pipeline or _PipelineStub(),  # type: ignore[arg-type]
        detector=AutoMeetingDetector(),
        repository=repo,
        event_bus=None,
        max_meeting_duration_s=max_meeting_duration_s,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_force_ends_stale_auto_meeting(tmp_path: Path) -> None:
    """auto-* 会议已超过 max_meeting_duration_s → hydrate 应强制结束并清空 _current。"""
    repo = await _make_repo(tmp_path)
    try:
        # seed：一条 2 小时前 start 的 auto-meeting，仍处 in_meeting
        stale_started = datetime.now(UTC) - timedelta(hours=2)
        await repo.create_meeting(
            "auto-1700000000",
            started_at=stale_started,
            auto_started=True,
        )

        state = _make_state(repo, max_meeting_duration_s=1800.0)
        await state.hydrate()

        assert state.current is None, "过期 auto-meeting 应被强制结束"

        rec = await repo.get_meeting("auto-1700000000")
        assert rec is not None
        assert rec.state == "ended", f"DB 状态应为 ended，实际为 {rec.state}"
        assert rec.ended_at is not None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_keeps_fresh_auto_meeting(tmp_path: Path) -> None:
    """auto-* 会议 started_at 在 max 之内 → hydrate 后保留为 current。"""
    repo = await _make_repo(tmp_path)
    try:
        fresh_started = datetime.now(UTC) - timedelta(minutes=5)
        await repo.create_meeting(
            "auto-1800000000",
            started_at=fresh_started,
            auto_started=True,
        )

        state = _make_state(repo, max_meeting_duration_s=1800.0)
        await state.hydrate()

        assert state.current is not None, "5 min 前的 auto-meeting 不应被结束"
        assert state.current.meeting_id == "auto-1800000000"
        assert state.current.started_by == "auto"

        rec = await repo.get_meeting("auto-1800000000")
        assert rec is not None
        assert rec.state == "in_meeting", "DB 中 fresh auto-meeting 应保持 in_meeting"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_keeps_old_manual_meeting(tmp_path: Path) -> None:
    """老的 manual 会议（不以 auto- 开头）任意年龄都不应被新规则误清。

    手动会议是用户显式创建的，顶栏对用户可见、可点击结束；
    所以不需要 max_duration 兜底。
    """
    repo = await _make_repo(tmp_path)
    try:
        old_started = datetime.now(UTC) - timedelta(hours=10)
        await repo.create_meeting(
            "m-deadbeef00",  # manual 命名（不以 auto- 开头）
            started_at=old_started,
            auto_started=False,
            title="超长 manual 会议",
        )

        state = _make_state(repo, max_meeting_duration_s=1800.0)
        await state.hydrate()

        assert state.current is not None
        assert state.current.meeting_id == "m-deadbeef00"
        assert state.current.started_by == "manual"

        rec = await repo.get_meeting("m-deadbeef00")
        assert rec is not None
        assert rec.state == "in_meeting"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observe_chunk_force_ends_hydrated_auto_meeting_after_max_duration(
    tmp_path: Path,
) -> None:
    """运行中兜底：hydrate 保留的 auto meeting 若之后超过 max，下一次 chunk 应自动结束。

    这覆盖截图里的真实问题：backend 重启后 ``MeetingState`` 恢复了
    ``auto-...`` current meeting，但 ``AutoMeetingDetector`` 丢了内存 start time；
    如果只有 detector 自己判断 max_duration，这场会一直 in_meeting，400+ 段也不
    生成纪要。
    """
    repo = await _make_repo(tmp_path)
    try:
        base_now = datetime.now(UTC)
        started = base_now - timedelta(minutes=29)
        await repo.create_meeting("auto-1900000000", started_at=started, auto_started=True)

        pipe = _FinalizingPipelineStub(repo)
        state = _make_state(repo, max_meeting_duration_s=1800.0, pipeline=pipe)
        await state.hydrate()
        assert state.current is not None

        effective_mid = await state.observe_chunk(
            speaker_id="speaker_1",
            duration_ms=1000,
            now=base_now + timedelta(minutes=2),
        )
        await state.await_pending_finalizations()

        assert effective_mid is None
        assert state.current is None
        assert pipe.finalized == [("auto-1900000000", "会议 auto-1900000000")]
        rec = await repo.get_meeting("auto-1900000000")
        assert rec is not None
        assert rec.state == "finalized"
        assert rec.minutes_status == "ok"
    finally:
        await repo.aclose()
