"""会议 API：开始/喂 chunk/结束。

设计上音频上传走 multipart（会议端实时切片 30s/段），纪要落地后通过
``/meetings/{id}/minutes`` 拉取，前端清单式展示。
"""

from __future__ import annotations

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
from app.schemas.meeting import MeetingMinutes, TranscriptSegment
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
