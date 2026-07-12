"""MeetingState.hydrate 单测：跨重启状态恢复 + 过期 auto-meeting 强制结束。

回归 2026-05 phase4-meeting-deadlock：
detector 进程崩溃 / 进程被杀重启时，sqlite 里残留的 auto-* in_meeting 行
会让顶栏继续显示"会议中 9h+"，但 detector 已经丢了内存状态、永远不会再
emit silence_timeout。修复办法：hydrate 时如果保留的最新会议是 auto-* 且
started_at 超过 max_meeting_duration_s，就强制 force-end 并把 _current=None。

手动会议允许在 24h 内跨重启续接；超过恢复上限必须结束，避免永久累计。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState

from tests.unit.test_meeting_pipeline import FakeDiarizer, FakeLLM, FakeRag, FakeSTT


class _PipelineStub:
    """State hydration also fences pipeline hydration after runtime recreation."""

    async def hydrate_from_repo(self) -> int:
        return 0

    async def start_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def end_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def finalize_meeting(self, *_a: Any, **_kw: Any) -> None:
        return None


async def _make_repo(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    return repo


def _make_state(
    repo: SQLiteRepository,
    *,
    max_meeting_duration_s: float = 1800.0,
    recovery_max_age_s: float = 24 * 60 * 60,
) -> MeetingState:
    return MeetingState(
        pipeline=_PipelineStub(),  # type: ignore[arg-type]
        detector=AutoMeetingDetector(),
        repository=repo,
        event_bus=None,
        max_meeting_duration_s=max_meeting_duration_s,
        recovery_max_age_s=recovery_max_age_s,
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
    """24h 恢复窗口内的 manual 会议跨重启后继续保持 current。

    10h 会议虽然很长，但仍可能是用户主动持续的工作，不应按 auto 的 30min
    上限误杀。
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
async def test_hydrate_force_ends_manual_meeting_older_than_recovery_limit(
    tmp_path: Path,
) -> None:
    """跨天仍 in_meeting 的普通手动会议必须结束，不能累计成数千分钟。"""
    repo = await _make_repo(tmp_path)
    try:
        stale_started = datetime.now(UTC) - timedelta(hours=48)
        await repo.create_meeting(
            "deploy-smoke-20260708172627",
            started_at=stale_started,
            auto_started=False,
            title="残留部署测试会议",
        )

        state = _make_state(
            repo,
            max_meeting_duration_s=1800.0,
            recovery_max_age_s=24 * 60 * 60,
        )
        await state.hydrate()

        assert state.current is None
        rec = await repo.get_meeting("deploy-smoke-20260708172627")
        assert rec is not None
        assert rec.state == "ended"
        assert rec.ended_at is not None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_start_after_runtime_recreation_reuses_durable_active_meeting(
    tmp_path: Path,
) -> None:
    """TTL eviction/restart must not expose a false idle or create a second row."""

    repo = await _make_repo(tmp_path)
    try:
        started_at = datetime.now(UTC) - timedelta(minutes=2)
        await repo.create_meeting(
            "m-existing-after-eviction",
            started_at=started_at,
            auto_started=False,
            title="驱逐前已开始",
        )

        recreated = _make_state(repo)
        current = await recreated.manual_start(title="不得创建第二场")

        assert current.meeting_id == "m-existing-after-eviction"
        assert current.started_at == started_at
        active = await repo.list_meetings(state="in_meeting", limit=10)
        assert [meeting.id for meeting in active] == ["m-existing-after-eviction"]
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_meeting_states_adopt_the_same_active_meeting(tmp_path: Path) -> None:
    """Two hydrated-idle runtimes must not return different meeting ids."""

    db_path = tmp_path / "two-states.db"
    repo_a = SQLiteRepository(db_path)
    repo_b = SQLiteRepository(db_path)
    await repo_a.init()
    await repo_b.init()
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )

    def pipeline(repo: SQLiteRepository) -> MeetingPipeline:
        return MeetingPipeline(
            settings=settings,
            stt=FakeSTT([]),
            diarizer=FakeDiarizer([]),
            rag=FakeRag(),
            llm=FakeLLM("{}"),
            repository=repo,
        )

    state_a = MeetingState(
        pipeline=pipeline(repo_a),
        detector=AutoMeetingDetector(),
        repository=repo_a,
    )
    state_b = MeetingState(
        pipeline=pipeline(repo_b),
        detector=AutoMeetingDetector(),
        repository=repo_b,
    )
    release = asyncio.Event()

    async def start(state: MeetingState, title: str):
        await release.wait()
        return await state.manual_start(title=title)

    try:
        # Make both process-local state machines cache the same idle snapshot.
        await asyncio.gather(state_a.hydrate(), state_b.hydrate())
        task_a = asyncio.create_task(start(state_a, "A"))
        task_b = asyncio.create_task(start(state_b, "B"))
        release.set()
        current_a, current_b = await asyncio.gather(task_a, task_b)

        assert current_a.meeting_id == current_b.meeting_id
        assert state_a.current == current_a
        assert state_b.current == current_b
        active = await repo_a.list_meetings(state="in_meeting", limit=10)
        assert [meeting.id for meeting in active] == [current_a.meeting_id]
    finally:
        await asyncio.gather(repo_a.aclose(), repo_b.aclose())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_recreation_hydrates_pipeline_before_new_chunk_and_finalize(
    tmp_path: Path,
) -> None:
    """Eviction must not make finalization omit the pre-eviction transcript."""

    repo = await _make_repo(tmp_path)
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    meeting_id = "m-runtime-recreated"
    try:
        before = MeetingPipeline(
            settings=settings,
            stt=FakeSTT([[TranscriptSegment(text="驱逐前的关键结论", start_ms=0, end_ms=500)]]),
            diarizer=FakeDiarizer(["speaker-a"]),
            rag=FakeRag(),
            llm=FakeLLM("{}"),
            repository=repo,
        )
        await before.start_meeting(meeting_id, title="跨驱逐会议")
        await before.add_audio_chunk(meeting_id, b"\x00\x00" * 16_000)

        llm = FakeLLM(
            '{"title":"完整纪要","summary":"两段都在",'
            '"sections":[{"heading":"结论","bullets":["旧段","新段"]}]}'
        )
        recreated_pipeline = MeetingPipeline(
            settings=settings,
            stt=FakeSTT([[TranscriptSegment(text="驱逐后的新增行动", start_ms=0, end_ms=500)]]),
            diarizer=FakeDiarizer(["speaker-a"]),
            rag=FakeRag(),
            llm=llm,
            repository=repo,
        )
        recreated_state = MeetingState(
            pipeline=recreated_pipeline,
            detector=AutoMeetingDetector(),
            repository=repo,
        )

        await recreated_state.hydrate()
        assert [s.text for s in recreated_pipeline.get_segments(meeting_id)] == ["驱逐前的关键结论"]

        await recreated_pipeline.add_audio_chunk(meeting_id, b"\x00\x00" * 16_000)
        await recreated_pipeline.finalize_meeting(meeting_id, title="跨驱逐会议")

        assert len(llm.calls) == 1
        prompt = "\n".join(str(message.content) for message in llm.calls[0]["messages"])
        assert "驱逐前的关键结论" in prompt
        assert "驱逐后的新增行动" in prompt
        persisted = await repo.list_meeting_segments(meeting_id)
        assert [segment.text for segment in persisted] == [
            "驱逐前的关键结论",
            "驱逐后的新增行动",
        ]
    finally:
        await repo.aclose()
