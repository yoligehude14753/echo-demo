"""会议 API：开始/喂 chunk/结束。

设计上音频上传走 multipart（会议端实时切片 30s/段），纪要落地后通过
``/meetings/{id}/minutes`` 拉取，前端清单式展示。

P4-M_meeting_history 新增（2026-05-28）：
- ``GET /meetings``                       前端启动期 hydrate 历史会议列表
- ``GET /meetings/{id}/transcript``       拉指定会议的转写段（``/segments`` 别名）
- ``GET /meetings/{id}/minutes``          反序列化 ``meetings.minutes_json``
- ``GET /meetings/{id}/artifacts``        per-meeting 产物（当前空，留扩展点）

artifacts 的产品决策（PR body 详述）：现 schema ``artifacts`` 无 meeting_id 列，
也没有 meeting_artifacts 关联表。前端 ``store.meetings[*].artifacts`` 是基于 WS
事件 ``artifact.ready.meeting_id`` 维护的 best-effort 视图。这个 endpoint 当前
返回空列表，**调用约定**保留以便后续接入数据库 join；前端在 currentMeetingId
被选中时仍以 store 内的 in-memory 列表为准。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.stt import make_stt
from app.api.deps import (
    get_diarizer_singleton,
    get_event_bus,
    get_llm_singleton,
    get_meeting_state,
    get_repository,
)
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.meeting import MeetingMinutes, MeetingSummary, TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState

router = APIRouter(prefix="/meetings", tags=["meetings"])

_pipeline: MeetingPipeline | None = None


def get_meeting_pipeline(
    settings: Settings = Depends(get_settings),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    repository: RepositoryPort = Depends(get_repository),
    diarizer: DiarizerPort = Depends(get_diarizer_singleton),
) -> MeetingPipeline:
    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        _pipeline = MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diarizer,
            rag=BM25Rag(settings),
            llm=llm,
            event_bus=event_bus,
            repository=repository,
        )
    return _pipeline


def get_meeting_pipeline_for_lifespan(
    settings: Settings,
    repository: RepositoryPort,
) -> MeetingPipeline:
    """lifespan 用：不通过 Depends 注入，直接拿单例（无 LLM/STT/RAG 也能 hydrate）。"""
    from app.api.deps import (
        get_diarizer_singleton as _get_diar,
    )
    from app.api.deps import (
        get_event_bus as _get_bus,
    )
    from app.api.deps import (
        get_llm_singleton as _get_llm,
    )

    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        bus = _get_bus()
        llm = _get_llm(settings)
        diar = _get_diar(settings)
        _pipeline = MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diar,
            rag=BM25Rag(settings),
            llm=llm,
            event_bus=bus,
            repository=repository,
        )
    return _pipeline


def reset_meeting_pipeline() -> None:
    """测试用：清掉缓存的单例。"""
    global _pipeline  # noqa: PLW0603
    _pipeline = None


@router.get("/current")
async def get_current_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> dict[str, object]:
    """全局会议状态机当前状态：idle 或 in_meeting。

    返回中带 ``minutes_status`` 让前端 MinutesView 能区分「会议中 / 生成中 / 失败 / 已生成」。
    in_meeting → minutes_status=null（会议没结束没纪要可言）
    idle      → 返回最新 meeting 的 minutes_status（若用户刚结束一个会议，UI 据此决定显示什么）
    """
    cur = state.current
    if cur is not None:
        return {
            "mode": "in_meeting",
            "meeting_id": cur.meeting_id,
            "started_at": cur.started_at.isoformat(),
            "started_by": cur.started_by,
            "minutes_status": None,
            "minutes_error": None,
        }
    # idle：探一下最近一条 meeting，把它的 minutes_status 透传出来
    latest = await repository.list_meetings(limit=1)
    latest_rec = latest[0] if latest else None
    return {
        "mode": "idle",
        "meeting_id": None,
        "started_at": None,
        "started_by": None,
        "minutes_status": latest_rec.minutes_status if latest_rec else None,
        "minutes_error": latest_rec.minutes_error if latest_rec else None,
    }


@router.post("/manual_start")
async def manual_start_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    title: str | None = Form(None),
) -> dict[str, object]:
    """用户点击状态栏：手动开始会议。已在会议中则原样返回。"""
    cur = await state.manual_start(title=title)
    return {
        "mode": "in_meeting",
        "meeting_id": cur.meeting_id,
        "started_at": cur.started_at.isoformat(),
        "started_by": cur.started_by,
    }


@router.post("/manual_end")
async def manual_end_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
) -> dict[str, object]:
    """用户点击状态栏：手动结束会议（含 finalize 纪要）。"""
    ended = await state.manual_end()
    return {"mode": "idle", "meeting_id": ended}


@router.get("", response_model=list[MeetingSummary])
async def list_meetings(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    limit: int = 50,
) -> list[MeetingSummary]:
    """会议列表（左侧面板用）。

    按 started_at DESC 倒序，每条带 segments / speakers 计数 + minutes 是否就绪，
    避免前端再发 N 次 detail 请求。

    这是前端启动期 hydrate 的核心入口：早于任何 ws 事件，让用户能马上看到历史
    会议；ws 事件随后只负责维护 in-progress 会议的实时增量。
    """
    rows = await repository.list_meetings(limit=limit)
    out: list[MeetingSummary] = []
    for r in rows:
        n_seg = await repository.count_meeting_segments(r.id)
        n_spk = await repository.count_meeting_speakers(r.id)
        out.append(
            MeetingSummary(
                meeting_id=r.id,
                title=r.title,
                display_title=r.display_title,  # M_minutes_refactor：语义化标题
                state=r.state,
                started_at=r.started_at,
                ended_at=r.ended_at,
                finalized_at=r.finalized_at,
                n_segments=n_seg,
                n_speakers=n_spk,
                has_minutes=bool(r.minutes_json),
            )
        )
    return out


@router.get("/{meeting_id}/transcript", response_model=list[TranscriptSegment])
async def get_transcript(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[TranscriptSegment]:
    """单会议转写流（中间面板用）。

    与 ``/segments`` 等价但语义更显式 + 直接走 repository（不依赖 pipeline 内
    存状态，可拉历史会议）。404 当会议不存在；空列表表示没有 segment 但会议
    本身存在（合法的"刚 start 还没说话"状态）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await repository.list_meeting_segments(meeting_id)


@router.get("/{meeting_id}/minutes", response_model=MeetingMinutes)
async def get_minutes(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> MeetingMinutes:
    """单会议纪要（右上面板用）。

    从 ``meetings.minutes_json`` 反序列化；finalize 之前会议没纪要时返回 404。
    JSON 解析失败抛 502（落库的纪要损坏属于运维问题，不应该让前端默默无展示）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not meeting.minutes_json:
        raise HTTPException(status_code=404, detail="minutes not generated yet")
    try:
        data = json.loads(meeting.minutes_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"minutes_json corrupted: {e!s}") from e
    # 早期落库的 minutes 可能没带 meeting_id；补上保持 schema 完整
    data.setdefault("meeting_id", meeting_id)
    return MeetingMinutes(**data)


@router.get("/{meeting_id}/artifacts", response_model=list[GeneratedArtifact])
async def get_meeting_artifacts(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[GeneratedArtifact]:
    """单会议产物（右下 outputs 面板用）。

    **当前实现**：返回空列表。原因：
    1. ``artifacts`` schema 没 meeting_id 列，也没有 ``meeting_artifacts`` 关
       联表。POST /artifacts/generate 不接受 meeting_id 参数。
    2. 前端 ``store.meetings[*].artifacts`` 是 best-effort 视图（基于 WS
       artifact.ready.meeting_id 字段维护，会话内有效）。
    3. 真正的"持久化 per-meeting outputs"需要在 ArtifactRequest / events /
       schema migration 三处一起改，超出 M_meeting_history 这个 PR 的范围。

    保留这个 endpoint 让前端调用约定稳定（``getMeetingArtifacts(id)`` 是 4 个
    detail endpoint 之一）；后续 PR 接 DB join 时只换实现，前端不动。

    会议不存在时仍返回 404（让前端能区分"无产物"与"无会议"）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return []


@router.post("/{meeting_id}/start")
async def start_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> dict[str, str]:
    """启动会议（low-level；建议走 /meetings/manual_start）。"""
    await pipeline.start_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "started"}


@router.post("/{meeting_id}/chunk", response_model=list[TranscriptSegment])
async def add_chunk(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
) -> list[TranscriptSegment]:
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        return await pipeline.add_audio_chunk(meeting_id, audio_bytes, sample_rate=sample_rate)
    except MeetingPipelineError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{meeting_id}/finalize", response_model=MeetingMinutes)
async def finalize(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    title: str = Form(...),
) -> MeetingMinutes:
    """生成或重试生成会议纪要（幂等）。

    幂等语义（2026-05-28 修）：
    - 第一次调用：用 segments + LLM 生成 minutes，写 ``state="finalized"`` + ``minutes_status="ok"``
    - 重试调用（前次失败 → ``state="ended"`` 且 ``minutes_status="generation_failed"``）：
      pipeline 重新装载 repo segments 并重新跑 LLM；成功覆盖原 minutes_json，
      失败再次写 ``generation_failed`` + 新的 ``minutes_error``。
    - 用户视角：「重试生成纪要」按钮就是再 POST 一次 ``/meetings/{id}/finalize``。
    """
    # 重试场景：pipeline 内存里没有这个 meeting 的 segments（重启 / 进程切换）
    # 显式装载一次，避免 finalize 报「no segments」。
    if not pipeline.get_segments(meeting_id):
        rec = await repository.get_meeting(meeting_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")
        loaded = await pipeline.load_meeting_for_retry(meeting_id)
        if not loaded:
            raise HTTPException(
                status_code=400,
                detail=f"meeting {meeting_id} has no segments to summarize",
            )
    try:
        return await pipeline.finalize_meeting(meeting_id, title=title)
    except MeetingPipelineError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{meeting_id}/end")
async def end_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> dict[str, str]:
    """结束会议叠加层（不生成纪要）；ambient 主链路继续。"""
    await pipeline.end_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "ended"}


@router.get("/{meeting_id}/segments", response_model=list[TranscriptSegment])
async def list_segments(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> list[TranscriptSegment]:
    return pipeline.get_segments(meeting_id)


@router.post("/{meeting_id}/inject_segment", response_model=TranscriptSegment)
async def inject_segment(
    meeting_id: str,
    seg: TranscriptSegment,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> TranscriptSegment:
    """演示/兜底入口：当 STT 服务不可用时直接注入已知转写片段。

    用途：
    - demo 录制：把预先准备的逐字稿喂进 pipeline，避开 STT 依赖
    - 离线回放：从 raw_transcript_ref 文件重放
    """
    return await pipeline.append_segment(meeting_id, seg)
