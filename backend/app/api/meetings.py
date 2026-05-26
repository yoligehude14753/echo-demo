"""会议 API：开始/喂 chunk/结束。

设计上音频上传走 multipart（会议端实时切片 30s/段），纪要落地后通过
``/meetings/{id}/minutes`` 拉取，前端清单式展示。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.adapters.diarizer import make_diarizer
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.stt.sensevoice_gpu import SenseVoiceGPUSTT
from app.api.deps import get_event_bus, get_llm_singleton
from app.config import Settings, get_settings
from app.schemas.meeting import MeetingMinutes, TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError

router = APIRouter(prefix="/meetings", tags=["meetings"])

_pipeline: MeetingPipeline | None = None


def get_meeting_pipeline(
    settings: Settings = Depends(get_settings),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> MeetingPipeline:
    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        _pipeline = MeetingPipeline(
            settings=settings,
            stt=SenseVoiceGPUSTT(settings),
            diarizer=make_diarizer(settings),
            rag=BM25Rag(settings),
            llm=llm,
            event_bus=event_bus,
        )
    return _pipeline


def reset_meeting_pipeline() -> None:
    """测试用：清掉缓存的单例。"""
    global _pipeline  # noqa: PLW0603
    _pipeline = None


@router.post("/{meeting_id}/start", status_code=204)
async def start_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> None:
    await pipeline.start_meeting(meeting_id)


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
    title: str = Form(...),
) -> MeetingMinutes:
    try:
        return await pipeline.finalize_meeting(meeting_id, title=title)
    except MeetingPipelineError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


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
