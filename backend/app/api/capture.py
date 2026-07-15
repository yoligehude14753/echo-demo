"""Ambient 主链路 API：POST /capture/chunk + GET /capture/stats。

每个 chunk 必走 ambient 质量门；仅有效语音落盘并进入 STT/RAG，可选 meeting_id
激活 meeting 叠加层。

M_diag_brake 新增：GET /capture/stats 返回进程级 7 道门处理结果计数，
供前端 CaptureStatus Popover 实时展示根因分布。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.adapters.stt import get_asr_scheduler, make_stt
from app.adapters.stt.llm_punctuator import LLMPunctuator
from app.adapters.stt.scheduler import ASRScheduler
from app.api.deps import (
    get_diarizer_singleton,
    get_event_bus,
    get_llm_singleton,
    get_meeting_state,
    get_quota_governor,
    get_repository,
    get_scope_runtime,
    get_speaker_registry,
    get_telemetry,
    reset_scope_runtime_component_for_test,
)
from app.api.meetings import get_meeting_pipeline
from app.api.memory import get_memory_dependency
from app.api.retrieval import get_rag
from app.config import Settings, get_settings
from app.memory import MemoryService
from app.ports.diarizer import DiarizerPort
from app.ports.asr import ASRRequestContext, ASRSchedulerPort, ASRTelemetryPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.schemas.capture import CaptureChunkResult
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor
from app.security.public_projection import project_client_dict
from app.telemetry.runtime import TelemetryRuntime
from app.upload import UploadTooLarge, read_limited_upload
from app.use_cases.ambient_capture import AmbientCapturePipeline
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

router = APIRouter(prefix="/capture", tags=["capture"])


def get_capture_asr_scheduler(
    settings: Settings = Depends(get_settings),
    telemetry: TelemetryRuntime = Depends(get_telemetry),
) -> ASRScheduler:
    return get_asr_scheduler(settings, telemetry=telemetry)


def _capture_asr_context(request: Request, settings: Settings) -> ASRRequestContext:
    """Build scheduler identity from the middleware-authenticated principal only."""

    principal = current_principal()
    idempotency_key = request.headers.get("Idempotency-Key", "").strip() or None
    request_id = request.headers.get("X-Request-ID", "").strip() or (f"capture-{uuid4().hex}")
    return ASRRequestContext(
        request_id=request_id,
        idempotency_key=idempotency_key,
        tenant_id=principal.tenant_id,
        principal_id=principal.user_id,
        device_id=principal.device_id,
        deadline_s=settings.asr_job_deadline_s,
        capability="ambient_capture",
        platform=request.headers.get("X-Echo-Platform") or "unknown",
        app_version=request.headers.get("X-Echo-App-Version") or "unknown",
    )


def get_ambient_pipeline(
    settings: Settings = Depends(get_settings),
    meeting: MeetingPipeline = Depends(get_meeting_pipeline),
    repository: RepositoryPort = Depends(get_repository),
    diarizer: DiarizerPort = Depends(get_diarizer_singleton),
    speaker_registry: SpeakerRegistry = Depends(get_speaker_registry),
    meeting_state: MeetingState = Depends(get_meeting_state),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
    rag: RagPort = Depends(get_rag),
    governor: PrincipalGovernor = Depends(get_quota_governor),
    memory: MemoryService = Depends(get_memory_dependency),
    asr_scheduler: ASRScheduler = Depends(get_capture_asr_scheduler),
    telemetry: TelemetryRuntime = Depends(get_telemetry),
) -> AmbientCapturePipeline:
    runtime = get_scope_runtime(settings)

    def make_pipeline() -> AmbientCapturePipeline:
        # text-clarity PR：把 LLM_FAST 包成 punctuator 注入。
        # 关闭开关只需要 AMBIENT_LLM_PUNCTUATE=false（settings）。
        punctuator = LLMPunctuator(llm, settings) if settings.ambient_llm_punctuate else None
        return AmbientCapturePipeline(
            settings=settings,
            stt=make_stt(settings),
            rag=rag,
            meeting=meeting,
            repository=repository,
            diarizer=diarizer,
            speaker_registry=speaker_registry,
            meeting_state=meeting_state,
            event_bus=event_bus,
            punctuator=punctuator,
            asr_scheduler=cast(ASRSchedulerPort, asr_scheduler),
            telemetry=cast(ASRTelemetryPort, telemetry),
            governor=governor,
            principal=current_principal(),
            memory=memory,
        )

    return runtime.get_or_create("ambient_pipeline", make_pipeline)


def reset_ambient_pipeline() -> None:
    reset_scope_runtime_component_for_test("ambient_pipeline")


@router.post("/chunk", response_model=CaptureChunkResult)
async def capture_chunk(
    request: Request,
    pipeline: Annotated[AmbientCapturePipeline, Depends(get_ambient_pipeline)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
    meeting_id: str | None = Form(None),
    settings: Settings = Depends(get_settings),
    governor: PrincipalGovernor = Depends(get_quota_governor),
) -> CaptureChunkResult:
    try:
        upload = await read_limited_upload(
            audio,
            max_bytes=int(settings.upload_max_file_mb * 1024 * 1024),
            chunk_bytes=settings.upload_read_chunk_bytes,
            governor=governor,
            principal=current_principal(),
            persistent=False,
            upload_reservation=getattr(request.state, "upload_quota_reservation", None),
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail="audio upload too large") from exc
    audio_bytes = upload.data
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    mid = meeting_id.strip() if meeting_id else None
    result = await pipeline.ingest_chunk(
        audio_bytes,
        sample_rate=sample_rate,
        meeting_id=mid or None,
        asr_context=_capture_asr_context(request, settings),
    )
    return CaptureChunkResult.model_validate(
        project_client_dict(result.model_dump(mode="json"), current_principal())
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
