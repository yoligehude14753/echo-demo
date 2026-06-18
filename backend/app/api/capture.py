"""Ambient 主链路 API：POST /capture/chunk + GET /capture/stats。

每个 chunk 必走 ambient（落盘 + STT + RAG）；可选 meeting_id 激活 meeting 叠加层。

M_diag_brake 新增：GET /capture/stats 返回进程级 7 道门处理结果计数，
供前端 CaptureStatus Popover 实时展示根因分布。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.stt import make_stt
from app.adapters.stt.llm_punctuator import LLMPunctuator
from app.api.deps import (
    get_diarizer_singleton,
    get_event_bus,
    get_llm_singleton,
    get_meeting_state,
    get_repository,
    get_speaker_registry,
)
from app.api.meetings import get_meeting_pipeline
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.schemas.capture import CaptureChunkResult
from app.use_cases.ambient_capture import AmbientCapturePipeline
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

router = APIRouter(prefix="/capture", tags=["capture"])

_ambient: AmbientCapturePipeline | None = None


def get_ambient_pipeline(
    settings: Settings = Depends(get_settings),
    meeting: MeetingPipeline = Depends(get_meeting_pipeline),
    repository: RepositoryPort = Depends(get_repository),
    diarizer: DiarizerPort = Depends(get_diarizer_singleton),
    speaker_registry: SpeakerRegistry = Depends(get_speaker_registry),
    meeting_state: MeetingState = Depends(get_meeting_state),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
) -> AmbientCapturePipeline:
    global _ambient  # noqa: PLW0603
    if _ambient is None:
        # text-clarity PR：把 LLM_FAST (qwen3.5-9b-local) 包成 punctuator 注入。
        # 关闭开关只需要 AMBIENT_LLM_PUNCTUATE=false（settings）。
        punctuator = LLMPunctuator(llm, settings) if settings.ambient_llm_punctuate else None
        _ambient = AmbientCapturePipeline(
            settings=settings,
            stt=make_stt(settings),
            rag=BM25Rag(settings),
            meeting=meeting,
            repository=repository,
            diarizer=diarizer,
            speaker_registry=speaker_registry,
            meeting_state=meeting_state,
            event_bus=event_bus,
            punctuator=punctuator,
        )
    return _ambient


def reset_ambient_pipeline() -> None:
    global _ambient  # noqa: PLW0603
    _ambient = None


@router.post("/chunk", response_model=CaptureChunkResult)
async def capture_chunk(
    pipeline: Annotated[AmbientCapturePipeline, Depends(get_ambient_pipeline)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
    meeting_id: str | None = Form(None),
) -> CaptureChunkResult:
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    mid = meeting_id.strip() if meeting_id else None
    return await pipeline.ingest_chunk(
        audio_bytes,
        sample_rate=sample_rate,
        meeting_id=mid or None,
    )


@router.get("/stats")
async def get_capture_stats(
    pipeline: Annotated[AmbientCapturePipeline, Depends(get_ambient_pipeline)],
) -> dict[str, object]:
    """ambient pipeline 7 道门处理结果分布（进程级 in-memory，重启清零）。

    供前端 CaptureStatus Popover 显示「哪道门把声音吃了」根因分布。
    所有计数器都是单调递增 int；客户端可定时轮询取差分得到瞬时速率。
    """
    return asdict(pipeline.get_stats())


@router.get("/recent")
async def list_recent_ambient(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    limit: int = 50,
) -> list[dict[str, object]]:
    """最近 N 条 ambient 转写片段（待机时 UI 转写流的数据源）。"""
    recs = await repository.list_ambient_segments(limit=limit)
    # 按时间正序（旧 → 新），符合用户阅读习惯
    recs_sorted = sorted(recs, key=lambda r: r.captured_at)
    return [
        {
            "text": r.text,
            "captured_at": r.captured_at.isoformat(),
            "speaker_id": r.speaker_id,
            "speaker_label": r.speaker_label,
            "duration_ms": r.duration_ms,
        }
        for r in recs_sorted
    ]
