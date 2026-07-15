"""MeetingState.manual_end / finalize 失败回归测试。

针对 echo-demo backend.log 2026-05-28 10:39:04 的根因：
    [WARNING] echodesk.meeting_state: manual_end finalize failed (still ending):
    MeetingPipeline.finalize_meeting() missing 1 required keyword-only argument: 'title'

验收场景（19-quality-detail.mdc 业务目标三问）：

1. **主路径**：manual_end 调 finalize_meeting 时 ``title`` 参数必传，
   且优先用 user 在 manual_start 时给的 title（不是 meeting_id 兜底）
2. **失败路径**：finalize 抛错时，会议状态进入 ``state="ended"`` +
   ``minutes_status="generation_failed"`` + 错误消息可读，**绝不卡在 "ending"**
3. **重试路径**：失败后 ``POST /meetings/{id}/finalize`` 幂等：装回 segments
   重跑 LLM；成功 → minutes_status="ok"，覆盖原失败状态
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.meetings import bind_meeting_workflow_handlers, dispatch_meeting_finalize
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse
from app.schemas.meeting import TranscriptSegment
from app.schemas.rag import RagChunk
from app.schemas.workflow import WorkflowRunCreate
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.workflows.kernel import WorkflowDispatcher
from app.workflows.service import (
    WorkflowConflictError,
    WorkflowService,
    new_workflow_run_id,
)

# ── stubs ──────────────────────────────────────────────────────────


class _STT:
    def __init__(self, queue: list[list[TranscriptSegment]]) -> None:
        self._q = list(queue)

    async def transcribe(
        self, _audio: bytes, *, sample_rate: int = 16_000, language: str = "zh"
    ) -> list[TranscriptSegment]:
        return self._q.pop(0) if self._q else []


class _Diar:
    def __init__(self, ids: list[str | None]) -> None:
        self._q = list(ids)

    async def identify(
        self,
        _a: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> str | None:
        return self._q.pop(0) if self._q else None

    async def reset(self) -> None:
        return None


class _Rag:
    async def ingest_pdf(self, *_a: Any, **_kw: Any) -> str:
        return "doc"

    async def ingest_meeting(self, *_a: Any, **_kw: Any) -> str:
        return "doc"

    async def query(self, *_a: Any, **_kw: Any) -> list[RagChunk]:
        return []

    async def delete(self, *_a: Any, **_kw: Any) -> None:
        return None


class _LLM:
    """可控的 LLM stub：第一次 .chat() 按 ``responses[0]`` 返回（或抛错），
    第二次按 ``responses[1]``，以此类推。允许模拟「先失败再重试成功」。
    """

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def chat(self, _msgs: list[ChatMessage], **_kw: Any) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise RuntimeError("LLM stub exhausted")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResponse(content=r, model="stub")

    async def chat_stream(self, _m: list[ChatMessage], **_kw: Any):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


class _BlockingLLM(_LLM):
    """让测试在 handler 已进入 LLM、但尚未完成时触发 startup restore。"""

    def __init__(self, response: str) -> None:
        super().__init__([response])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, _msgs: list[ChatMessage], **_kw: Any) -> LLMResponse:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return LLMResponse(content=self._responses.pop(0), model="stub")


class _BlockingTerminalLLM(_LLM):
    def __init__(self, outcome: str | Exception) -> None:
        super().__init__([outcome])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, _msgs: list[ChatMessage], **_kw: Any) -> LLMResponse:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(content=outcome, model="stub")


def _good_minutes_json() -> str:
    return json.dumps(
        {
            "summary": "讨论 Q3 预算",
            "sections": [{"heading": "议题1", "bullets": ["要点 A", "要点 B"]}],
            "decisions": ["砍 30%"],
            "action_items": ["Alice 周五前出修订版"],
        },
        ensure_ascii=False,
    )


async def _wait_for_background_finalize(state: MeetingState) -> None:
    """测试显式等待异步纪要；production 的 manual_end 必须先返回 ended。"""

    tasks = tuple(state._finalize_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def _minutes_failed_outbox_rows(
    db_path: Path,
) -> list[tuple[str, dict[str, Any], str | None]]:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            """SELECT aggregate_id, payload_json, published_at
               FROM workflow_outbox
               WHERE aggregate_type = 'domain' AND event_type = 'minutes.failed'
               ORDER BY outbox_id"""
        )
        rows = await cur.fetchall()
        await cur.close()
    return [
        (str(row[0]), json.loads(str(row[1])), str(row[2]) if row[2] is not None else None)
        for row in rows
    ]


async def _seed_v37_unfinished_finalize(
    tmp_path: Path,
    *,
    expired: bool,
    cleared: bool = False,
) -> tuple[Settings, str, str]:
    suffix = "cleared" if cleared else ("expired" if expired else "running")
    db_path = tmp_path / f"v37-{suffix}.db"
    v37_catalog = tmp_path / f"migrations-v37-{suffix}"
    v37_catalog.mkdir()
    for source in _DEFAULT_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"):
        if int(source.name.split("_", 1)[0]) <= 37:
            shutil.copy2(source, v37_catalog / source.name)
    legacy = await run_migrations(db_path, migrations_dir=v37_catalog)
    assert legacy.errors == [] and legacy.current_version == 37

    now = datetime.now(UTC)
    meeting_id = f"m-v37-{suffix}"
    run_id = f"wf-v37-{suffix}"
    deadline = now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, title, state, started_at, ended_at, minutes_status, minutes_error,
                tenant_id, device_id, owner_id)
               VALUES (?, ?, 'ended', ?, ?, 'generating', 'process exited',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id, "v37 恢复纪要", now.isoformat(), now.isoformat()),
        )
        await conn.execute(
            """INSERT INTO meeting_segments
               (meeting_id, text, start_ms, end_ms, captured_at,
                tenant_id, device_id, owner_id)
               VALUES (?, '旧版未完成纪要', 0, 800, ?,
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id, now.isoformat()),
        )
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, title, intent_text, meeting_id,
                input_json, output_json, error, timeout_s, created_at, started_at,
                finished_at, updated_at, tenant_id, device_id, owner_id, revision,
                idempotency_key, attempt, parent_run_id, deadline_at,
                cancel_requested_at, active_key)
               VALUES (?, 'meeting.finalize', 'v37-crash', 'running', ?, ?, ?,
                       ?, '{}', NULL, 300, ?, ?, NULL, ?,
                       'legacy-local', 'legacy-local', 'legacy-local', 0,
                       ?, 1, NULL, ?, NULL, ?)""",
            (
                run_id,
                "v37 恢复纪要",
                f"Finalize meeting {meeting_id}",
                meeting_id,
                json.dumps({"meeting_id": meeting_id, "title": "v37 恢复纪要"}),
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
                f"meeting.finalize:{meeting_id}:v37",
                deadline.isoformat(),
                f"meeting.finalize:{meeting_id}",
            ),
        )
        if cleared:
            await conn.execute(
                """UPDATE meetings
                   SET minutes_status = NULL, minutes_error = '', minutes_cleared_at = ?
                   WHERE id = ?""",
                ((now - timedelta(seconds=30)).isoformat(), meeting_id),
            )
        await conn.commit()

    upgraded = await run_migrations(db_path)
    assert upgraded.errors == [] and upgraded.current_version == 41
    return (
        Settings(
            db_path=db_path,
            storage_dir=tmp_path / "storage",
            rag_index_dir=tmp_path / "rag",
            _env_file=None,  # type: ignore[call-arg]
        ),
        meeting_id,
        run_id,
    )


async def _seed_meeting(
    tmp_path: Path,
    *,
    title: str | None,
    llm_responses: list[str | Exception],
) -> tuple[SQLiteRepository, MeetingPipeline, MeetingState, str]:
    """共用 fixture：创建 repo + pipeline + state；用户 manual_start 落库 title；
    喂一段 segments；返回 meeting_id。
    """
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    llm = _LLM(llm_responses)
    pipe = MeetingPipeline(
        settings=settings,
        stt=_STT([[TranscriptSegment(text="今天讨论 Q3 预算", start_ms=0, end_ms=2000)]]),
        diarizer=_Diar(["spk-A"]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    state = MeetingState(
        pipeline=pipe,
        detector=AutoMeetingDetector(),
        repository=repo,
    )
    cur = await state.manual_start(title=title)
    await pipe.add_audio_chunk(cur.meeting_id, b"\x00" * 16_000)
    return repo, pipe, state, cur.meeting_id


# ── 测试 1: 主路径：title 必传 ──────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_end_passes_title_kwarg_no_missing_arg(tmp_path: Path) -> None:
    """回归 backend.log: ``finalize_meeting() missing 1 required keyword-only argument: 'title'``。

    用 spy 拦截 pipeline.finalize_meeting 看 title 是否被传入。
    """
    repo, pipe, state, mid = await _seed_meeting(
        tmp_path,
        title="Q3 销售评审",
        llm_responses=[_good_minutes_json()],
    )
    try:
        captured: dict[str, Any] = {}
        orig_finalize = pipe.finalize_meeting

        async def spy_finalize(meeting_id: str, **kw: Any):  # type: ignore[no-untyped-def]
            captured["title"] = kw.get("title")
            captured["meeting_id"] = meeting_id
            return await orig_finalize(meeting_id, **kw)

        pipe.finalize_meeting = spy_finalize  # type: ignore[assignment]

        ended = await state.manual_end()
        assert ended == mid
        immediate = await repo.get_meeting(mid)
        assert immediate is not None and immediate.state == "ended"
        await _wait_for_background_finalize(state)
        assert captured["title"] == "Q3 销售评审", (
            "manual_end 必须把 user 在 manual_start 时给的 title 传给 finalize_meeting，"
            f"实际：{captured.get('title')!r}"
        )
        assert captured["meeting_id"] == mid

        # DB：state 已 finalized + minutes_status="ok"
        rec = await repo.get_meeting(mid)
        assert rec is not None
        assert rec.state == "finalized"
        assert rec.minutes_status == "ok"
        assert rec.minutes_json is not None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_end_title_fallback_to_meeting_id_when_no_user_title(
    tmp_path: Path,
) -> None:
    """user 在 manual_start 时没给 title → fallback 用 ``"会议 <id>"``，不直接用裸 id。"""
    repo, pipe, state, mid = await _seed_meeting(
        tmp_path, title=None, llm_responses=[_good_minutes_json()]
    )
    try:
        captured: dict[str, Any] = {}
        orig = pipe.finalize_meeting

        async def spy(meeting_id: str, **kw: Any):  # type: ignore[no-untyped-def]
            captured["title"] = kw.get("title")
            return await orig(meeting_id, **kw)

        pipe.finalize_meeting = spy  # type: ignore[assignment]
        await state.manual_end()
        await _wait_for_background_finalize(state)
        # 兜底命名包含 meeting_id，但前缀是中文，避免直接显示 m-xxx
        assert captured["title"] == f"会议 {mid}"
    finally:
        await repo.aclose()


# ── 测试 2: 失败路径：LLM 失败不卡死 ────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_end_does_not_mark_generating_before_workflow_dispatch(
    tmp_path: Path,
) -> None:
    """创建 durable workflow run 前不能提交不可恢复的 ``generating`` 状态。

    如果进程恰好在状态写入和 workflow dispatch 之间退出，数据库里会留下一个
    没有 run 可以 restore 的假进行中会议。callback 代表 workflow dispatch 边界：
    它被调用时，会议必须仍保持可由 hydrate 恢复的 ``in_meeting`` 状态。
    """
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    pipe = MeetingPipeline(
        settings=settings,
        stt=_STT([[TranscriptSegment(text="崩溃窗口回归", start_ms=0, end_ms=800)]]),
        diarizer=_Diar(["spk-A"]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
        repository=repo,
    )
    observed: dict[str, str | None] = {}

    async def finalize_via_workflow(meeting_id: str, title: str) -> object:
        before_dispatch = await repo.get_meeting(meeting_id)
        assert before_dispatch is not None
        observed["state"] = before_dispatch.state
        observed["minutes_status"] = before_dispatch.minutes_status
        return await pipe.finalize_meeting(meeting_id, title=title)

    state = MeetingState(
        pipeline=pipe,
        detector=AutoMeetingDetector(),
        repository=repo,
        finalize_callback=finalize_via_workflow,
    )
    try:
        cur = await state.manual_start(title="崩溃窗口回归")
        await pipe.add_audio_chunk(cur.meeting_id, b"\x00" * 16_000)

        assert await state.manual_end() == cur.meeting_id
        await _wait_for_background_finalize(state)
        assert observed == {"state": "ended", "minutes_status": None}

        completed = await repo.get_meeting(cur.meeting_id)
        assert completed is not None
        assert completed.state == "finalized"
        assert completed.minutes_status == "ok"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_end_does_not_mark_generating_before_workflow_dispatch(
    tmp_path: Path,
) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    pipe = MeetingPipeline(
        settings=Settings(db_path=tmp_path / "echo.db", storage_dir=tmp_path / "storage"),
        stt=_STT([[TranscriptSegment(text="自动会议崩溃窗口", start_ms=0, end_ms=800)]]),
        diarizer=_Diar(["spk-A"]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
        repository=repo,
    )
    observed: dict[str, str | None] = {}

    async def finalize_via_workflow(meeting_id: str, title: str) -> object:
        before_dispatch = await repo.get_meeting(meeting_id)
        assert before_dispatch is not None
        observed["state"] = before_dispatch.state
        observed["minutes_status"] = before_dispatch.minutes_status
        return await pipe.finalize_meeting(meeting_id, title=title)

    state = MeetingState(
        pipeline=pipe,
        detector=AutoMeetingDetector(),
        repository=repo,
        finalize_callback=finalize_via_workflow,
    )
    try:
        meeting_id = "auto-crash-window"
        await state._apply_auto_start(meeting_id, reason="test")
        await pipe.add_audio_chunk(meeting_id, b"\x00" * 16_000)

        await state._apply_auto_end(meeting_id, reason="silence_timeout")
        await _wait_for_background_finalize(state)

        assert observed == {"state": "ended", "minutes_status": None}
        completed = await repo.get_meeting(meeting_id)
        assert completed is not None
        assert completed.state == "finalized"
        assert completed.minutes_status == "ok"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_end_finalize_failure_marks_generation_failed(tmp_path: Path) -> None:
    """LLM 抛错 → 会议进入 ``state="ended"`` + ``minutes_status="generation_failed"``，
    带 minutes_error，**不卡在 in_meeting**，UI 能识别给重试入口。
    """
    repo, _pipe, state, mid = await _seed_meeting(
        tmp_path,
        title="Q3 销售评审",
        llm_responses=[RuntimeError("yunwu 502 bad gateway")],
    )
    try:
        # finalize 失败时 manual_end 仍然成功返回 meeting_id（不抛给上层）
        ended = await state.manual_end()
        assert ended == mid
        await _wait_for_background_finalize(state)

        rec = await repo.get_meeting(mid)
        assert rec is not None
        assert rec.state == "ended", (
            f"finalize 失败不应卡在 in_meeting，期望 'ended'，实际 {rec.state!r}"
        )
        assert rec.minutes_status == "generation_failed"
        assert rec.minutes_error is not None
        assert "yunwu" in rec.minutes_error
        assert rec.minutes_json is None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_end_emits_minutes_failed_event_on_llm_error(tmp_path: Path) -> None:
    """LLM 失败时必须发 ``minutes.failed`` WS 事件，前端据此切到「失败 · 重试」UI。"""
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    events: list[Any] = []

    class _Bus:
        async def publish(self, ev: Any) -> None:
            events.append(ev)

    try:
        pipe = MeetingPipeline(
            settings=settings,
            stt=_STT([[TranscriptSegment(text="hi", start_ms=0, end_ms=400)]]),
            diarizer=_Diar(["spk-A"]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([RuntimeError("connection refused")]),  # type: ignore[arg-type]
            event_bus=_Bus(),  # type: ignore[arg-type]
            repository=repo,
        )
        state = MeetingState(
            pipeline=pipe,
            detector=AutoMeetingDetector(),
            repository=repo,
            event_bus=_Bus(),  # type: ignore[arg-type]
        )
        cur = await state.manual_start(title="测试会议")
        await pipe.add_audio_chunk(cur.meeting_id, b"\x00" * 16_000)
        await state.manual_end()
        await _wait_for_background_finalize(state)

        failed = [e for e in events if e.type == "minutes.failed"]
        assert len(failed) == 1, f"应发一条 minutes.failed，实际：{[e.type for e in events]}"
        assert failed[0].meeting_id == cur.meeting_id
        assert "connection refused" in str(failed[0].payload.get("error", ""))
    finally:
        await repo.aclose()


# ── 测试 3: 重试路径：POST /finalize 幂等 ───────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_finalize_covers_previous_failure(tmp_path: Path) -> None:
    """第一次 finalize 失败 → 第二次（重试）成功 → minutes_status="ok" 覆盖失败。

    模拟用户场景：纪要生成失败 → 看到「重试」按钮 → 点击 → 后端再跑 LLM 成功。
    """
    repo, pipe, state, mid = await _seed_meeting(
        tmp_path,
        title="Q3 评审",
        llm_responses=[RuntimeError("first call fails"), _good_minutes_json()],
    )
    try:
        # 第一次：manual_end 触发 finalize → 失败 → minutes_status="generation_failed"
        await state.manual_end()
        await _wait_for_background_finalize(state)
        rec = await repo.get_meeting(mid)
        assert rec is not None
        assert rec.minutes_status == "generation_failed"

        # 第二次：用户点「重试」→ pipeline.finalize_meeting 直接再跑一次（segments 还在内存）
        minutes = await pipe.finalize_meeting(mid, title="Q3 评审")
        assert minutes.title == "Q3 评审"
        assert "Q3" in minutes.summary

        rec2 = await repo.get_meeting(mid)
        assert rec2 is not None
        assert rec2.state == "finalized"
        assert rec2.minutes_status == "ok"
        assert rec2.minutes_json is not None
        # 错误消息被「清空」（用空串显式覆盖；DB 上反映为 ""）
        assert (rec2.minutes_error or "") == ""
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_finalize_after_restart_loads_segments_from_repo(tmp_path: Path) -> None:
    """模拟：第一次失败后 backend 重启 → pipeline 内存清空 → 重试时必须从 repo
    重新装载 segments 才能跑 LLM。
    """
    # 进程 1：创建会议、跑失败的 finalize
    repo1 = SQLiteRepository(tmp_path / "echo.db")
    await repo1.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    try:
        pipe1 = MeetingPipeline(
            settings=settings,
            stt=_STT([[TranscriptSegment(text="segments seed", start_ms=0, end_ms=400)]]),
            diarizer=_Diar(["spk-A"]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([RuntimeError("first fail")]),  # type: ignore[arg-type]
            repository=repo1,
        )
        state1 = MeetingState(
            pipeline=pipe1,
            detector=AutoMeetingDetector(),
            repository=repo1,
        )
        cur = await state1.manual_start(title="重启重试")
        mid = cur.meeting_id
        await pipe1.add_audio_chunk(mid, b"\x00" * 16_000)
        await state1.manual_end()
        await _wait_for_background_finalize(state1)
        rec = await repo1.get_meeting(mid)
        assert rec is not None
        assert rec.minutes_status == "generation_failed"
    finally:
        await repo1.aclose()

    # 进程 2：新 pipeline 内存空；走 load_meeting_for_retry → finalize 成功
    repo2 = SQLiteRepository(tmp_path / "echo.db")
    await repo2.init()
    try:
        pipe2 = MeetingPipeline(
            settings=settings,
            stt=_STT([]),
            diarizer=_Diar([]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
            repository=repo2,
        )
        # 内存里没有 segments
        assert pipe2.get_segments(mid) == []
        loaded = await pipe2.load_meeting_for_retry(mid)
        assert loaded is True
        assert len(pipe2.get_segments(mid)) >= 1

        minutes = await pipe2.finalize_meeting(mid, title="重启重试")
        assert minutes.summary
        rec2 = await repo2.get_meeting(mid)
        assert rec2 is not None
        assert rec2.state == "finalized"
        assert rec2.minutes_status == "ok"
    finally:
        await repo2.aclose()


# ── 测试 4: 启动恢复（hydrate 卡死会议）─────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recover_stuck_minutes_retries_failed_meetings(tmp_path: Path) -> None:
    """backend 启动时，``state="ended"`` 且无 minutes_json 的卡死会议应被自动重试。

    模拟用户那个 ``m-bdd1da4e7e21``：finalize 失败后卡在「已结束 · 14 人 · 29 段」
    但纪要永远不出现。下次启动 backend，``recover_stuck_minutes`` 应主动救活。
    """
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    try:
        # seed: 卡死的会议（state=ended，无 minutes_json，无 minutes_status — legacy）
        mid = "m-stuck-legacy"
        await repo.create_meeting(mid, started_at=datetime.now(UTC), title="历史卡死会议")
        await repo.append_meeting_segment(
            mid,
            TranscriptSegment(
                text="历史 segment",
                start_ms=0,
                end_ms=1000,
                speaker_id="spk-X",
                speaker_label="说话人1",
            ),
            captured_at=datetime.now(UTC),
        )
        # 模拟 manual_end 后 finalize 失败留下来的状态
        await repo.update_meeting_state(mid, state="ended", ended_at=datetime.now(UTC))

        # 新 pipeline + state（模拟 backend 重启）
        pipe = MeetingPipeline(
            settings=settings,
            stt=_STT([]),
            diarizer=_Diar([]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
            repository=repo,
        )
        state = MeetingState(
            pipeline=pipe,
            detector=AutoMeetingDetector(),
            repository=repo,
        )

        n = await state.recover_stuck_minutes()
        assert n == 1, f"应尝试 1 个卡死会议，实际 {n}"

        rec = await repo.get_meeting(mid)
        assert rec is not None
        assert rec.state == "finalized"
        assert rec.minutes_status == "ok"
        assert rec.minutes_json is not None
        loaded = json.loads(rec.minutes_json)
        assert loaded["title"] == "历史卡死会议"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recover_stuck_minutes_skips_durable_user_clear_after_restart(
    tmp_path: Path,
) -> None:
    """A deliberate minutes clear is not a stuck legacy failure after restart."""

    db_path = tmp_path / "echo.db"
    repo = SQLiteRepository(db_path)
    await repo.init()
    meeting_id = "m-explicitly-cleared"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="用户清理纪要")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="不应自动再生成", start_ms=0, end_ms=1000),
        captured_at=datetime.now(UTC),
    )
    await repo.update_meeting_state(
        meeting_id,
        state="finalized",
        minutes_json=_good_minutes_json(),
        minutes_status="ok",
    )
    await repo.clear_meeting_outputs(meeting_id, clear_minutes=True)
    cleared = await repo.get_meeting(meeting_id)
    assert cleared is not None and cleared.minutes_cleared_at is not None
    await repo.aclose()

    restarted_repo = SQLiteRepository(db_path)
    await restarted_repo.init()
    llm = _LLM([_good_minutes_json()])
    pipeline = MeetingPipeline(
        settings=Settings(db_path=db_path, storage_dir=tmp_path / "storage"),
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=restarted_repo,
    )
    state = MeetingState(
        pipeline=pipeline,
        detector=AutoMeetingDetector(),
        repository=restarted_repo,
    )
    try:
        assert await state.recover_stuck_minutes() == 0
        assert llm.call_count == 0
        still_cleared = await restarted_repo.get_meeting(meeting_id)
        assert still_cleared is not None
        assert still_cleared.minutes_json is None
        assert still_cleared.minutes_cleared_at is not None
    finally:
        await restarted_repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_recover_before_restore_reuses_unfinished_finalize_run(
    tmp_path: Path,
) -> None:
    """recover 先调度、restore 后进入时，只能恢复同一 run 并生成一次纪要。"""
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-startup-dedupe"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="启动恢复去重")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="只应生成一次", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    await repo.update_meeting_state(
        meeting_id,
        state="ended",
        ended_at=datetime.now(UTC),
        minutes_status="generation_failed",
        minutes_error="process exited",
    )

    llm = _BlockingLLM(_good_minutes_json())
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    original = await workflow.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="pre-crash",
            title="启动恢复去重",
            intent_text=f"Finalize meeting {meeting_id}",
            meeting_id=meeting_id,
            input={"meeting_id": meeting_id, "title": "启动恢复去重"},
            timeout_s=300,
            idempotency_key=f"meeting.finalize:{meeting_id}",
        )
    )

    async def finalize_via_workflow(target_id: str, title: str) -> object:
        return await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=target_id,
            title=title,
            source="startup_recover",
        )

    state = MeetingState(
        pipeline=pipeline,
        detector=AutoMeetingDetector(),
        repository=repo,
        finalize_callback=finalize_via_workflow,
    )
    recover_task = asyncio.create_task(state.recover_stuck_minutes())
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        assert await dispatcher.restore_unfinished() == 1
        llm.release.set()
        assert await recover_task == 1

        done = await dispatcher.wait(original.run_id)
        assert done is not None
        assert done.state == "succeeded"
        assert llm.call_count == 1
        runs = await workflow.list_runs(meeting_id=meeting_id)
        assert [run.run_id for run in runs] == [original.run_id]
    finally:
        llm.release.set()
        if not recover_task.done():
            recover_task.cancel()
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_v37_finalize_restore_attaches_generation_owner_before_llm(
    tmp_path: Path,
) -> None:
    settings, meeting_id, run_id = await _seed_v37_unfinished_finalize(
        tmp_path,
        expired=False,
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    llm = _BlockingLLM(_good_minutes_json())
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    try:
        assert await dispatcher.restore_unfinished() == 1
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        attached = await repo.get_meeting(meeting_id)
        assert attached is not None
        assert attached.minutes_status == "generating"
        assert attached.minutes_generation_run_id == run_id

        llm.release.set()
        done = await asyncio.wait_for(dispatcher.wait_succeeded(run_id), timeout=2)
        assert done.state == "succeeded"
        assert llm.call_count == 1
        finalized = await repo.get_meeting(meeting_id)
        assert finalized is not None
        assert finalized.state == "finalized"
        assert finalized.minutes_status == "ok"
        assert finalized.minutes_generation_run_id is None
    finally:
        llm.release.set()
        await dispatcher.aclose()
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_v37_finalize_restore_projects_terminal_without_llm(
    tmp_path: Path,
) -> None:
    settings, meeting_id, run_id = await _seed_v37_unfinished_finalize(
        tmp_path,
        expired=True,
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    llm = _LLM([_good_minutes_json()])
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    try:
        assert await dispatcher.restore_unfinished() == 1
        done = await asyncio.wait_for(dispatcher.wait(run_id), timeout=2)
        assert done is not None and done.state == "timeout"
        assert llm.call_count == 0
        meeting = await repo.get_meeting(meeting_id)
        assert meeting is not None
        assert meeting.state == "ended"
        assert meeting.minutes_status == "generation_failed"
        assert "超时" in (meeting.minutes_error or "")
        assert meeting.minutes_generation_run_id is None
        failed_events = await _minutes_failed_outbox_rows(settings.db_path)
        assert len(failed_events) == 1
        assert failed_events[0][0] == run_id
    finally:
        await dispatcher.aclose()
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_v37_finalize_restore_never_revives_cleared_minutes(
    tmp_path: Path,
) -> None:
    settings, meeting_id, run_id = await _seed_v37_unfinished_finalize(
        tmp_path,
        expired=False,
        cleared=True,
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    before = await repo.get_meeting(meeting_id)
    assert before is not None and before.minutes_cleared_at is not None
    cleared_at = before.minutes_cleared_at
    llm = _LLM([_good_minutes_json()])
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    try:
        assert await dispatcher.restore_unfinished() == 1
        done = await asyncio.wait_for(dispatcher.wait(run_id), timeout=2)
        assert done is not None and done.state == "failed"
        assert llm.call_count == 0
        still_cleared = await repo.get_meeting(meeting_id)
        assert still_cleared is not None
        assert still_cleared.state == "ended"
        assert still_cleared.minutes_status is None
        assert still_cleared.minutes_cleared_at == cleared_at
        assert still_cleared.minutes_generation_run_id is None
        assert await _minutes_failed_outbox_rows(settings.db_path) == []
    finally:
        await dispatcher.aclose()
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_workflow_finalize_failure_creates_real_retry_attempt(tmp_path: Path) -> None:
    """A terminal failed run must not poison the meeting's permanent request key."""
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-workflow-finalize-retry"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="纪要真实重试")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="第一次失败后应真正重试", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    llm = _LLM([RuntimeError("provider unavailable"), _good_minutes_json()])
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    try:
        with pytest.raises(MeetingPipelineError, match="provider unavailable"):
            await dispatch_meeting_finalize(
                dispatcher,
                pipeline,
                repo,
                meeting_id=meeting_id,
                title="纪要真实重试",
                source="retry-test",
            )

        minutes = await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=meeting_id,
            title="纪要真实重试",
            source="retry-test",
        )

        assert minutes.summary
        assert llm.call_count == 2
        runs = await workflow.list_runs(meeting_id=meeting_id)
        assert len(runs) == 2
        succeeded, failed = runs
        assert succeeded.state == "succeeded"
        assert succeeded.parent_run_id == failed.run_id
        assert succeeded.attempt == 2
        assert failed.state == "failed"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_after_more_than_200_historical_runs_uses_unique_run_identity(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-finalize-history-window"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="历史窗口")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="第 202 次也必须创建新 run", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.executemany(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, meeting_id,
                idempotency_key, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES (?, 'meeting.finalize', 'history', 'succeeded',
                       'historic finalize', ?, ?, '2026-01-01', '2026-01-01',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            [
                (
                    f"historic-finalize-{index:03d}",
                    meeting_id,
                    f"meeting.finalize:{meeting_id}:generation:{index}",
                )
                for index in range(1, 202)
            ],
        )
        await conn.commit()

    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    try:
        minutes = await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=meeting_id,
            title="历史窗口",
            source="history-window-test",
        )

        assert minutes.summary
        async with aiosqlite.connect(settings.db_path) as conn:
            cur = await conn.execute(
                """SELECT run_id, idempotency_key FROM workflow_runs
                   WHERE meeting_id = ? AND source = 'history-window-test'""",
                (meeting_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        assert len(rows) == 1
        assert str(rows[0][1]) == f"meeting.finalize:{meeting_id}:run:{rows[0][0]}"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meeting_retry_conflict_waits_for_fresh_authoritative_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-fresh-wins-retry"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="权威赢家")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="fresh winner should be awaited", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=_LLM([RuntimeError("first attempt failed"), _good_minutes_json()]),  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    try:
        with pytest.raises(MeetingPipelineError, match="first attempt failed"):
            await dispatch_meeting_finalize(
                dispatcher,
                pipeline,
                repo,
                meeting_id=meeting_id,
                title="权威赢家",
                source="winner-test",
            )

        original_retry = dispatcher.retry

        async def fresh_wins(_run_id: str, **_kwargs: Any) -> None:
            fresh_id = new_workflow_run_id()

            async def write_marker(conn: aiosqlite.Connection) -> None:
                await conn.execute(
                    """UPDATE meetings
                       SET state = 'ended', minutes_status = 'generating',
                           minutes_generation_run_id = ?,
                           minutes_generation_cancelled_at = NULL
                       WHERE id = ? AND tenant_id = 'legacy-local'
                         AND owner_id = 'legacy-local'""",
                    (fresh_id, meeting_id),
                )

            await dispatcher.dispatch_atomic(
                WorkflowRunCreate(
                    kind="meeting.finalize",
                    source="fresh-winner",
                    intent_text=f"Finalize meeting {meeting_id}",
                    meeting_id=meeting_id,
                    input={"meeting_id": meeting_id, "title": "fresh winner"},
                    timeout_s=300,
                    idempotency_key=f"meeting.finalize:{meeting_id}:fresh-winner",
                    active_key=f"meeting.finalize:{meeting_id}",
                ),
                domain_writer=write_marker,
                run_id=fresh_id,
            )
            raise WorkflowConflictError("fresh winner committed first")

        monkeypatch.setattr(dispatcher, "retry", fresh_wins)
        minutes = await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=meeting_id,
            title="请求中的重试",
            source="retry-loser",
        )
        monkeypatch.setattr(dispatcher, "retry", original_retry)

        assert minutes.summary
        runs = await workflow.list_runs(meeting_id=meeting_id)
        assert len(runs) == 2
        assert runs[0].state == "succeeded"
        assert runs[0].source == "fresh-winner"
        assert runs[1].state == "failed"
        meeting = await repo.get_meeting(meeting_id)
        assert meeting is not None and meeting.minutes_status == "ok"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_cancel_is_terminal_until_an_explicit_retry_supersedes_it(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-explicit-cancel"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="取消纪要")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="这次生成将被取消", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    blocking_llm = _BlockingLLM(_good_minutes_json())
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=blocking_llm,  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    finalize_task = asyncio.create_task(
        dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=meeting_id,
            title="取消纪要",
            source="cancel-test",
        )
    )
    try:
        await asyncio.wait_for(blocking_llm.started.wait(), timeout=1)
        [active] = await workflow.list_runs(meeting_id=meeting_id)
        cancelled = await dispatcher.cancel(active.run_id, reason="user cancelled")
        assert cancelled is not None and cancelled.state == "cancelled"
        with pytest.raises(MeetingPipelineError):
            await finalize_task

        cancelled_meeting = await repo.get_meeting(meeting_id)
        assert cancelled_meeting is not None
        assert cancelled_meeting.state == "ended"
        assert cancelled_meeting.minutes_status == "generation_failed"
        assert cancelled_meeting.minutes_generation_run_id is None
        assert cancelled_meeting.minutes_generation_cancelled_at is not None
        cancelled_events = await _minutes_failed_outbox_rows(settings.db_path)
        assert len(cancelled_events) == 1
        assert cancelled_events[0][0] == active.run_id
        assert cancelled_events[0][1] == {
            "meeting_id": meeting_id,
            "payload": {"error": "会议纪要生成已取消"},
        }
        assert cancelled_events[0][2] is not None

        recovery = MeetingState(
            pipeline=pipeline,
            detector=AutoMeetingDetector(),
            repository=repo,
        )
        assert await recovery.recover_stuck_minutes() == 0
        assert blocking_llm.call_count == 1

        retry_pipeline = MeetingPipeline(
            settings=settings,
            stt=_STT([]),
            diarizer=_Diar([]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
            repository=repo,
        )
        minutes = await dispatch_meeting_finalize(
            dispatcher,
            retry_pipeline,
            repo,
            meeting_id=meeting_id,
            title="取消后显式重试",
            source="cancel-retry-test",
        )
        assert minutes.summary
        recovered = await repo.get_meeting(meeting_id)
        assert recovered is not None
        assert recovered.state == "finalized"
        assert recovered.minutes_status == "ok"
        assert recovered.minutes_generation_run_id is None
        assert recovered.minutes_generation_cancelled_at is None

        # A delayed terminal callback from the old run cannot overwrite the
        # successful retry because the domain projection is run-id owned.
        projector = dispatcher.registry.resolve_terminal_projector(
            "meeting.finalize",
            ("legacy-local", "legacy-local"),
        )
        old = await workflow.get_run(active.run_id)
        assert projector is not None and old is not None
        async with aiosqlite.connect(settings.db_path) as conn:
            await projector(conn, old, "cancelled")
            await conn.commit()
        still_recovered = await repo.get_meeting(meeting_id)
        assert still_recovered is not None
        assert still_recovered.state == "finalized"
        assert still_recovered.minutes_status == "ok"
        assert len(await _minutes_failed_outbox_rows(settings.db_path)) == 1
    finally:
        blocking_llm.release.set()
        if not finalize_task.done():
            finalize_task.cancel()
            await asyncio.gather(finalize_task, return_exceptions=True)
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_timeout_atomically_projects_owned_meeting_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "m-finalize-timeout"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="超时纪要")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="模型一直没有返回", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=_LLM([_good_minutes_json()]),  # type: ignore[arg-type]
        repository=repo,
    )
    workflow = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(workflow)
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def blocking_handler(
        _context: Any,
        _payload: dict[str, Any],
    ) -> dict[str, Any]:
        handler_started.set()
        await handler_release.wait()
        return {}

    dispatcher.registry.register(
        "meeting.finalize",
        blocking_handler,
        scope=("legacy-local", "legacy-local"),
        replace=True,
    )
    monkeypatch.setattr(dispatcher, "_remaining_timeout", lambda _run: 0.2)
    run_id = new_workflow_run_id()

    async def write_marker(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            """UPDATE meetings
               SET state = 'ended', minutes_status = 'generating',
                   minutes_generation_run_id = ?
               WHERE id = ? AND tenant_id = 'legacy-local' AND owner_id = 'legacy-local'""",
            (run_id, meeting_id),
        )

    await workflow.create_run_atomic(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="timeout-test",
            intent_text=f"Finalize meeting {meeting_id}",
            meeting_id=meeting_id,
            input={"meeting_id": meeting_id, "title": "超时纪要"},
            timeout_s=300,
            idempotency_key=f"meeting.finalize:{meeting_id}:timeout",
            active_key=f"meeting.finalize:{meeting_id}",
        ),
        domain_writer=write_marker,
        run_id=run_id,
    )
    try:
        assert await dispatcher.restore_unfinished() == 1
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        done = await asyncio.wait_for(dispatcher.wait(run_id), timeout=2)
        assert done is not None and done.state == "timeout"
        meeting = await repo.get_meeting(meeting_id)
        assert meeting is not None
        assert meeting.state == "ended"
        assert meeting.minutes_status == "generation_failed"
        assert meeting.minutes_generation_run_id is None
        assert meeting.minutes_generation_cancelled_at is None
        assert "超时" in (meeting.minutes_error or "")
        timeout_events = await _minutes_failed_outbox_rows(settings.db_path)
        assert len(timeout_events) == 1
        assert timeout_events[0][0] == run_id
        assert timeout_events[0][1] == {
            "meeting_id": meeting_id,
            "payload": {"error": "会议纪要生成超时"},
        }
        assert timeout_events[0][2] is not None
    finally:
        handler_release.set()
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "timeout"])
async def test_stale_finalize_terminal_never_revives_cleared_meeting_or_emits_minutes_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    settings = Settings(
        db_path=tmp_path / f"{terminal}.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = f"m-stale-{terminal}"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="过期终态")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="清理后旧任务不得复活", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    outcome: str | Exception = (
        RuntimeError("stale llm failure") if terminal == "failed" else _good_minutes_json()
    )
    llm = _BlockingTerminalLLM(outcome)
    pipeline = MeetingPipeline(
        settings=settings,
        stt=_STT([]),
        diarizer=_Diar([]),
        rag=_Rag(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        repository=repo,
    )
    bus = InMemoryEventBus()
    workflow = WorkflowService(settings, bus)
    dispatcher = WorkflowDispatcher(workflow)
    if terminal == "timeout":
        monkeypatch.setattr(dispatcher, "_remaining_timeout", lambda _run: 1.0)
    task = asyncio.create_task(
        dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repo,
            meeting_id=meeting_id,
            title="过期终态",
            source="stale-terminal-test",
        )
    )
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        [run] = await workflow.list_runs(meeting_id=meeting_id)
        before_clear = await repo.get_meeting(meeting_id)
        assert before_clear is not None
        assert before_clear.minutes_generation_run_id == run.run_id
        await repo.clear_meeting_outputs(meeting_id)

        if terminal == "failed":
            llm.release.set()
        elif terminal == "cancelled":
            cancelled = await dispatcher.cancel(run.run_id, reason="clear won")
            assert cancelled is not None and cancelled.state == "cancelled"
        with pytest.raises(MeetingPipelineError):
            await asyncio.wait_for(task, timeout=2)

        done = await workflow.get_run(run.run_id)
        assert done is not None and done.state == terminal
        cleared = await repo.get_meeting(meeting_id)
        assert cleared is not None
        assert cleared.state == "ended"
        assert cleared.minutes_json is None
        assert cleared.minutes_status is None
        assert cleared.minutes_cleared_at is not None
        assert cleared.minutes_generation_run_id is None
        assert cleared.rag_projection_state == "delete_pending"
        assert cleared.rag_projection_generation == before_clear.rag_projection_generation + 1
        assert await _minutes_failed_outbox_rows(settings.db_path) == []
    finally:
        llm.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recover_stuck_minutes_persists_failure_state_when_retry_fails(
    tmp_path: Path,
) -> None:
    """重启时 retry 又失败 → 状态保持 ``generation_failed`` + 新错误消息，
    用户下次打开 UI 仍然能看到「重试」按钮（不会被改成 ok）。
    """
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    settings = Settings(storage_dir=tmp_path / "storage")
    try:
        mid = "m-stuck-still-failing"
        await repo.create_meeting(mid, started_at=datetime.now(UTC), title="网络一直挂的会议")
        await repo.append_meeting_segment(
            mid,
            TranscriptSegment(text="x", start_ms=0, end_ms=400),
            captured_at=datetime.now(UTC),
        )
        await repo.update_meeting_state(
            mid,
            state="ended",
            ended_at=datetime.now(UTC),
            minutes_status="generation_failed",
            minutes_error="prev error",
        )

        pipe = MeetingPipeline(
            settings=settings,
            stt=_STT([]),
            diarizer=_Diar([]),
            rag=_Rag(),  # type: ignore[arg-type]
            llm=_LLM([RuntimeError("network still down")]),  # type: ignore[arg-type]
            repository=repo,
        )
        state = MeetingState(
            pipeline=pipe,
            detector=AutoMeetingDetector(),
            repository=repo,
        )
        await state.recover_stuck_minutes()

        rec = await repo.get_meeting(mid)
        assert rec is not None
        assert rec.state == "ended"
        assert rec.minutes_status == "generation_failed"
        assert rec.minutes_error is not None
        assert "network still down" in rec.minutes_error
    finally:
        await repo.aclose()
