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

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse
from app.schemas.meeting import TranscriptSegment
from app.schemas.rag import RagChunk
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState

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

    async def identify(self, _a: bytes, *, sample_rate: int = 16_000) -> str | None:
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
        # 兜底命名包含 meeting_id，但前缀是中文，避免直接显示 m-xxx
        assert captured["title"] == f"会议 {mid}"
    finally:
        await repo.aclose()


# ── 测试 2: 失败路径：LLM 失败不卡死 ────────────────────────────────────


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
