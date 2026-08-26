"""Ambient 主链路 API：POST /capture/chunk + GET /capture/stats。

每个 chunk 必走 ambient 质量门；仅有效语音落盘并进入 STT/RAG，可选 meeting_id
激活 meeting 叠加层。

M_diag_brake 新增：GET /capture/stats 返回进程级 7 道门处理结果计数，
供前端 CaptureStatus Popover 实时展示根因分布。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

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
from app.hub.runtime import HubRuntimeError
from app.memory import MemoryService
from app.ports.asr import ASRRequestContext, ASRSchedulerPort, ASRTelemetryPort
from app.ports.diarizer import DiarizerPort
from app.ports.rag import RagPort
from app.ports.repository import (
    CaptureClaimLost,
    CaptureIdempotencyConflict,
    CaptureRequestClaim,
    RepositoryPort,
)
from app.runtime.capture_selection import CaptureSelectionStore
from app.schemas.capture import (
    CaptureAdmissionReceipt,
    CaptureAuthorizeRequest,
    CaptureChunkResult,
    CaptureControlUpdate,
    CaptureStreamMode,
    SttStatus,
)
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor
from app.security.models import Principal
from app.security.public_projection import project_client_dict
from app.telemetry.runtime import TelemetryRuntime
from app.upload import UploadTooLarge, read_limited_upload
from app.use_cases.ambient_capture import AmbientCapturePipeline, AmbientPersistenceError
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

router = APIRouter(prefix="/capture", tags=["capture"])
logger = logging.getLogger(__name__)

_CAPTURE_IDEMPOTENCY_CONFLICT_HEADER = "X-Capture-Idempotency-Conflict"
_CAPTURE_IDEMPOTENCY_PENDING_HEADER = "X-Capture-Idempotency-Pending"
_CAPTURE_IDEMPOTENCY_REPLAY_HEADER = "X-Capture-Idempotent-Replay"
_CAPTURE_ERROR_CLASS_HEADER = "X-Capture-Error-Class"
_CAPTURE_IDENTITY_MAX_LENGTH = 256


@dataclass(frozen=True, slots=True)
class _CaptureIdentity:
    operation_key_hash: str
    client_segment_hash: str
    idempotency_key_hash: str | None
    request_fingerprint: str


def _capture_hash(namespace: str, value: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"echodesk-capture-v1\0")
    digest.update(namespace.encode("ascii"))
    digest.update(b"\0")
    digest.update(value)
    return digest.hexdigest()


def _capture_identity(
    *,
    audio_bytes: bytes,
    sample_rate: int,
    meeting_id: str | None,
    capture_mode: str,
    client_segment_id: str,
    idempotency_key: str | None,
) -> _CaptureIdentity:
    client_segment_hash = _capture_hash("client-segment", client_segment_id.encode())
    idempotency_key_hash = (
        _capture_hash("idempotency-key", idempotency_key.encode())
        if idempotency_key is not None
        else None
    )
    operation_key_hash = _capture_hash(
        "operation-idempotency" if idempotency_key is not None else "operation-segment",
        (idempotency_key or client_segment_id).encode(),
    )
    fingerprint_input = json.dumps(
        {
            "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "sample_rate": sample_rate,
            "meeting_id": meeting_id,
            "capture_mode": capture_mode,
            "client_segment_hash": client_segment_hash,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return _CaptureIdentity(
        operation_key_hash=operation_key_hash,
        client_segment_hash=client_segment_hash,
        idempotency_key_hash=idempotency_key_hash,
        request_fingerprint=_capture_hash("request-fingerprint", fingerprint_input),
    )


def _capture_lease_seconds(settings: Settings) -> float:
    return min(300.0, max(30.0, float(settings.asr_job_deadline_s) + 30.0))


async def _claim_capture_request(
    repository: RepositoryPort,
    *,
    identity: _CaptureIdentity,
    holder_id: str,
    lease_seconds: float,
    meeting_id: str | None,
    capture_mode: str,
) -> CaptureRequestClaim:
    """等待并发胜者，或在其 lease 过期后以新 fence 接管。"""

    # Durable clients retry with the same identity. Do not hold one HTTP request
    # for an entire provider lease: a short bounded wait avoids client timeout
    # while still giving an almost-complete winner a chance to publish receipt.
    deadline = time.monotonic() + min(5.0, max(1.0, lease_seconds))
    while True:
        claim = await repository.claim_capture_request(
            operation_key_hash=identity.operation_key_hash,
            client_segment_hash=identity.client_segment_hash,
            idempotency_key_hash=identity.idempotency_key_hash,
            request_fingerprint=identity.request_fingerprint,
            holder_id=holder_id,
            lease_seconds=lease_seconds,
            meeting_id=meeting_id,
            capture_mode=capture_mode,
        )
        if claim.status != "in_progress":
            return claim
        if time.monotonic() >= deadline:
            logger.warning(
                "capture_error_class=capture_claim_in_progress source_branch=_claim_capture_request"
            )
            raise HTTPException(
                status_code=503,
                detail="capture request is still being processed",
                headers={
                    _CAPTURE_IDEMPOTENCY_PENDING_HEADER: "1",
                    _CAPTURE_ERROR_CLASS_HEADER: "capture_claim_in_progress",
                    "Retry-After": "1",
                },
            )
        until_expiry = max(0.01, claim.lease_expires_at - time.time())
        await asyncio.sleep(min(0.1, until_expiry))


async def _renew_capture_claim(
    repository: RepositoryPort,
    claim: CaptureRequestClaim,
    *,
    lease_seconds: float,
    claim_lost: asyncio.Event,
) -> None:
    interval = max(1.0, min(10.0, lease_seconds / 3))
    lease_deadline = claim.lease_expires_at
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await repository.renew_capture_request_claim(
                claim,
                lease_seconds=lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient SQLite scheduling/lock error is not proof that the
            # fence was lost. Retry within the last confirmed lease window;
            # only an actual CAS miss or elapsed lease declares ownership lost.
            if time.time() >= lease_deadline:
                claim_lost.set()
                return
            continue
        if renewed is None:
            claim_lost.set()
            return
        lease_deadline = renewed.lease_expires_at


async def _rebuild_capture_result(
    repository: RepositoryPort,
    receipt: CaptureRequestClaim,
    *,
    client_segment_id: str,
) -> CaptureChunkResult:
    ambient = (
        await repository.get_ambient_segment(receipt.ambient_segment_id)
        if receipt.ambient_segment_id is not None
        else None
    )
    meeting_segments = (
        await repository.list_capture_meeting_segments(
            receipt.meeting_id,
            capture_operation_key=receipt.operation_key_hash,
        )
        if receipt.meeting_id is not None
        else []
    )
    stt_status: SttStatus = (
        cast(SttStatus, receipt.stt_status)
        if receipt.stt_status
        in {
            "ok",
            "empty",
            "failed",
            "circuit_open",
            "gated",
            "terminal_ignored",
            "unknown",
        }
        else "unknown"
    )
    capture_mode: CaptureStreamMode = (
        cast(CaptureStreamMode, receipt.capture_mode)
        if receipt.capture_mode in {"free", "formal", "auto"}
        else "free"
    )
    return CaptureChunkResult(
        segment_id=client_segment_id,
        ambient_segment_id=receipt.ambient_segment_id,
        ambient_stored=receipt.ambient_stored,
        ambient_text=ambient.text if ambient is not None else None,
        audio_ref=ambient.audio_ref if ambient is not None else "",
        speaker_id=ambient.speaker_id if ambient is not None else None,
        speaker_label=ambient.speaker_label if ambient is not None else None,
        meeting_id=receipt.meeting_id,
        meeting_segments=meeting_segments,
        stt_status=stt_status,
        capture_mode=capture_mode,
        admission=_capture_admission(receipt),
    )


def _capture_admission(receipt: CaptureRequestClaim) -> CaptureAdmissionReceipt:
    """把内部 hash receipt 投影为稳定的短 opaque id；不向客户端暴露完整 hash。"""

    return CaptureAdmissionReceipt(receipt_id=f"capture-{receipt.operation_key_hash[:16]}")


def _project_capture_result(
    result: CaptureChunkResult,
    *,
    principal: Principal,
    capture_session_id: str,
    source: Literal["desktop", "device"],
) -> CaptureChunkResult:
    payload = project_client_dict(result.model_dump(mode="json"), principal)
    payload["device_id"] = principal.device_id
    payload["capture_session_id"] = capture_session_id
    payload["source"] = source
    return CaptureChunkResult.model_validate(payload)


def _resolve_capture_scope(
    *,
    principal_session_id: str,
    capture_session_id: str | None,
    source: Literal["desktop", "device"] | None,
) -> tuple[str, Literal["desktop", "device"]]:
    """Normalize a capture attribution without rejecting released desktop clients.

    EchoDesk 0.3.5 predates the client-side capture scope fields.  Its
    authenticated requests therefore contain neither field, even though the
    server can still bind the upload to the authenticated device principal.
    Accept only that *complete* legacy shape and synthesize a server-bound
    desktop scope.  Any partial or empty modern scope remains invalid so a
    malformed request cannot bypass the current fail-closed correlation rule.
    """

    if capture_session_id is None and source is None:
        # ``principal_session_id`` is server-issued, never a client claim.  It
        # scopes the compatibility response to this authenticated session
        # without exposing a reusable bearer or relaxing device ownership.
        return (f"legacy-session-{principal_session_id}"[:128], "desktop")
    if capture_session_id is None or source is None:
        raise HTTPException(
            status_code=422,
            detail="captureSessionId and source must be provided together",
        )
    normalized_session_id = capture_session_id.strip()
    if not normalized_session_id or len(normalized_session_id) > 128:
        raise HTTPException(status_code=422, detail="captureSessionId is required")
    return normalized_session_id, source


def get_capture_selection_store(
    settings: Settings = Depends(get_settings),
) -> CaptureSelectionStore:
    return CaptureSelectionStore(settings.db_path)


@router.get("/devices")
async def get_capture_devices(request: Request) -> dict[str, object]:
    runtime = getattr(request.app.state, "hub_runtime", None)
    raw_devices: list[dict[str, object]] = []
    if runtime is not None and runtime.settings.hub_enabled and runtime.configured:
        try:
            raw_devices = await runtime.list_devices()
        except HubRuntimeError as exc:
            raise HTTPException(status_code=503, detail="设备列表暂不可用") from exc
    return {
        "devices": [
            {
                "deviceId": item.get("device_id"),
                "displayName": item.get("name"),
                "platform": item.get("platform"),
                "online": item.get("status") == "online",
                "lastSeenAt": item.get("last_seen_at"),
            }
            for item in raw_devices
        ]
    }


@router.get("/control")
async def get_capture_control(
    store: CaptureSelectionStore = Depends(get_capture_selection_store),
) -> dict[str, object]:
    principal = current_principal()
    return (await store.get(principal.tenant_id, principal.owner_id)).payload()


@router.put("/control")
async def put_capture_control(
    body: CaptureControlUpdate,
    store: CaptureSelectionStore = Depends(get_capture_selection_store),
) -> dict[str, object]:
    principal = current_principal()
    try:
        selection = await store.update(
            principal.tenant_id,
            principal.owner_id,
            mode=body.mode,
            selected_device_ids=body.selectedDeviceIds,
            expected_revision=body.expectedRevision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return selection.payload()


@router.post("/control/authorize")
async def authorize_capture(
    body: CaptureAuthorizeRequest,
    store: CaptureSelectionStore = Depends(get_capture_selection_store),
) -> dict[str, object]:
    principal = current_principal()
    if body.deviceId != principal.device_id:
        raise HTTPException(status_code=403, detail="device identity mismatch")
    selection = await store.get(principal.tenant_id, principal.owner_id)
    return {
        "allowed": body.revision == selection.revision and selection.allows(body.deviceId),
        "mode": selection.mode,
        "revision": selection.revision,
    }


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
    response: Response,
    pipeline: Annotated[AmbientCapturePipeline, Depends(get_ambient_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
    meeting_id: str | None = Form(None),
    device_id: str = Form(..., alias="deviceId"),
    segment_id: str = Form(..., alias="segmentId"),
    capture_session_id: str | None = Form(None, alias="captureSessionId"),
    source: Literal["desktop", "device"] | None = Form(None),
    capture_mode: Literal["free", "formal"] = Form("free"),
    captured_at_ms: int | None = Form(None, ge=0),
    settings: Settings = Depends(get_settings),
    governor: PrincipalGovernor = Depends(get_quota_governor),
    selection_store: CaptureSelectionStore = Depends(get_capture_selection_store),
) -> CaptureChunkResult:
    principal = current_principal()
    if device_id != principal.device_id:
        raise HTTPException(status_code=403, detail="device identity mismatch")
    normalized_segment_id = segment_id.strip()
    if not normalized_segment_id:
        raise HTTPException(status_code=422, detail="segmentId is required")
    if len(normalized_segment_id) > _CAPTURE_IDENTITY_MAX_LENGTH:
        raise HTTPException(status_code=422, detail="segmentId is too long")
    idempotency_key = request.headers.get("Idempotency-Key", "").strip() or None
    if idempotency_key is not None and len(idempotency_key) > _CAPTURE_IDENTITY_MAX_LENGTH:
        raise HTTPException(status_code=422, detail="Idempotency-Key is too long")
    normalized_capture_session_id, normalized_source = _resolve_capture_scope(
        principal_session_id=principal.session_id,
        capture_session_id=capture_session_id,
        source=source,
    )
    selection = await selection_store.get(principal.tenant_id, principal.owner_id)
    if not selection.allows(principal.device_id):
        raise HTTPException(status_code=409, detail="capture is not selected for this device")
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
    identity = _capture_identity(
        audio_bytes=audio_bytes,
        sample_rate=sample_rate,
        meeting_id=mid or None,
        capture_mode=capture_mode,
        client_segment_id=normalized_segment_id,
        idempotency_key=idempotency_key,
    )
    lease_seconds = _capture_lease_seconds(settings)
    try:
        claim = await _claim_capture_request(
            repository,
            identity=identity,
            holder_id=f"capture-{uuid4().hex}",
            lease_seconds=lease_seconds,
            meeting_id=mid,
            capture_mode=capture_mode,
        )
    except CaptureIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="capture idempotency key conflicts with a different request",
            headers={_CAPTURE_IDEMPOTENCY_CONFLICT_HEADER: "1"},
        ) from exc

    if claim.status == "completed":
        response.headers[_CAPTURE_IDEMPOTENCY_REPLAY_HEADER] = "1"
        replay = await _rebuild_capture_result(
            repository,
            claim,
            client_segment_id=normalized_segment_id,
        )
        return _project_capture_result(
            replay,
            principal=principal,
            capture_session_id=normalized_capture_session_id,
            source=normalized_source,
        )

    claim_lost = asyncio.Event()
    service_started_at = pipeline.begin_request()
    service_succeeded = False
    heartbeat = asyncio.create_task(
        _renew_capture_claim(
            repository,
            claim,
            lease_seconds=lease_seconds,
            claim_lost=claim_lost,
        ),
        name=f"capture-claim-heartbeat:{claim.operation_key_hash[:12]}",
    )
    release_claim = True
    try:
        result = await pipeline.ingest_chunk(
            audio_bytes,
            sample_rate=sample_rate,
            meeting_id=mid or None,
            capture_mode=capture_mode,
            asr_context=_capture_asr_context(request, settings),
            client_segment_id=normalized_segment_id,
            capture_operation_key=claim.operation_key_hash,
            request_fingerprint=claim.request_fingerprint,
            captured_at=(
                datetime.fromtimestamp(captured_at_ms / 1000, UTC)
                if captured_at_ms is not None
                else None
            ),
        )
        # Heartbeat failure is only an observation. The transactional
        # holder+fence CAS below is the authority: it can still commit after
        # deadline when no takeover won, and rejects a genuinely stale owner.
        completed_claim = await repository.complete_capture_request(
            claim,
            ambient_stored=result.ambient_stored,
            ambient_segment_id=result.ambient_segment_id,
            meeting_id=result.meeting_id,
            stt_status=result.stt_status,
            capture_mode=result.capture_mode,
        )
        # Durable receipt 只能在 holder+fence CAS 真正提交后返回；此前的
        # pipeline result 仍是内部结果，不能让客户端据此删除本地分片。
        result = result.model_copy(update={"admission": _capture_admission(completed_claim)})
        release_claim = False
        service_succeeded = True
    except CaptureIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="capture idempotency key conflicts with a different request",
            headers={_CAPTURE_IDEMPOTENCY_CONFLICT_HEADER: "1"},
        ) from exc
    except CaptureClaimLost as exc:
        logger.warning(
            "capture_error_class=capture_claim_lost source_branch=completion_fence_cas"
        )
        raise HTTPException(
            status_code=503,
            detail="capture request ownership changed; retry is safe",
            headers={
                _CAPTURE_IDEMPOTENCY_PENDING_HEADER: "1",
                _CAPTURE_ERROR_CLASS_HEADER: "capture_claim_lost",
                "Retry-After": "1",
            },
        ) from exc
    except AmbientPersistenceError as exc:
        logger.warning(
            "capture_error_class=ambient_persistence_unavailable source_branch=ambient_persistence"
        )
        raise HTTPException(
            status_code=503,
            detail="ambient persistence unavailable",
            headers={_CAPTURE_ERROR_CLASS_HEADER: "ambient_persistence_unavailable"},
        ) from exc
    finally:
        pipeline.finish_request(service_started_at, success=service_succeeded)
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        if release_claim:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await repository.release_capture_request_claim(claim)
    return _project_capture_result(
        result,
        principal=principal,
        capture_session_id=normalized_capture_session_id,
        source=normalized_source,
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
            "device_id": getattr(r, "device_id", None),
            "segment_id": getattr(r, "client_segment_id", None),
        }
        for r in recs_sorted
    ]
