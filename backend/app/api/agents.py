"""EchoDesk agent task API。

客户端只和 EchoDesk 后端交互，不直连 AgentOS；普通 UI 也不展示 provider 名称。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.intent.llm_router import LLMIntentRouter
from app.agents.artifact_transfer import (
    ArtifactContentLengthError,
    ArtifactSizeLimitError,
    validated_content_length,
)
from app.agents.artifact_transfer import (
    bounded_artifact_body as _bounded_artifact_body,
)
from app.agents.artifact_transfer import (
    close_artifact_stream as _close_artifact_proxy,
)
from app.agents.base import AgentIntent
from app.agents.service import (
    PROFILE_FULL_ACCESS,
    RUNNER_CLAUDE_CODE,
    AgentRunnerGrant,
    AgentTaskRecord,
    AgentTaskService,
    get_agent_task_service,
)
from app.api.deps import get_event_bus, require_admin_access
from app.api.deps import get_llm_singleton as get_llm
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.security.context import current_principal
from app.security.headers import PRIVATE_NO_STORE_HEADERS
from app.security.public_projection import project_client_dict, server_private_roots

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentTaskCreateRequest(BaseModel):
    device_id: str = "desktop"
    text: str
    title: str | None = None
    task_kind: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None


class AgentTaskDTO(BaseModel):
    task_id: str
    runner_task_id: str | None = None
    device_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    title: str
    intent_text: str
    route: str
    task_kind: str | None = None
    state: str
    progress_text: str = ""
    final_text: str | None = None
    error: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    workflow_run_id: str | None = None
    last_seq: int = 0
    submitted_at: str
    finished_at: str | None = None
    timeout_s: float

    @classmethod
    def from_record(cls, rec: AgentTaskRecord) -> AgentTaskDTO:
        return cls(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            device_id=rec.device_id,
            conversation_id=rec.conversation_id,
            message_id=rec.message_id,
            title=rec.title,
            intent_text=rec.intent_text,
            route="agent_task",
            task_kind=rec.task_kind,
            state=rec.state.value,
            progress_text=rec.progress_text,
            final_text=rec.final_text,
            error=rec.error,
            artifacts=rec.artifacts,
            snapshot=rec.snapshot,
            workflow_run_id=rec.workflow_run_id,
            last_seq=rec.last_seq,
            submitted_at=rec.submitted_at,
            finished_at=rec.finished_at,
            timeout_s=rec.timeout_s,
        )


class AgentTaskEventsDTO(BaseModel):
    task_id: str
    events: list[dict[str, Any]]
    snapshot: dict[str, Any]
    last_seq: int


class GrantDTO(BaseModel):
    grant_id: str
    device_id: str
    runner: str
    permission_profile: str
    permission_mode: str
    workspace_ids: list[str]
    granted_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None

    @classmethod
    def from_grant(cls, grant: AgentRunnerGrant) -> GrantDTO:
        return cls(
            grant_id=grant.grant_id,
            device_id=grant.device_id,
            runner=grant.runner,
            permission_profile=grant.permission_profile,
            permission_mode=grant.permission_mode,
            workspace_ids=grant.workspace_ids,
            granted_at=grant.granted_at,
            revoked_at=grant.revoked_at,
            last_used_at=grant.last_used_at,
        )


class GrantStatusDTO(BaseModel):
    grant: GrantDTO | None = None


class GrantCreateRequest(BaseModel):
    device_id: str
    workspace_ids: list[str] = Field(default_factory=list)
    permission_profile: str = PROFILE_FULL_ACCESS
    resume_task_id: str | None = None


class GrantCreateResponse(BaseModel):
    grant: GrantDTO
    resumed_task: AgentTaskDTO | None = None


def _task_dto(rec: AgentTaskRecord, settings: Settings) -> AgentTaskDTO:
    dto = AgentTaskDTO.from_record(rec)
    return AgentTaskDTO.model_validate(
        project_client_dict(
            dto.model_dump(mode="json"),
            current_principal(),
            private_roots=server_private_roots(settings),
        )
    )


def _task_events_dto(
    task_id: str,
    *,
    events: list[dict[str, Any]],
    snapshot: dict[str, Any],
    last_seq: int,
    settings: Settings,
) -> AgentTaskEventsDTO:
    return AgentTaskEventsDTO.model_validate(
        project_client_dict(
            {
                "task_id": task_id,
                "events": events,
                "snapshot": snapshot,
                "last_seq": last_seq,
            },
            current_principal(),
            private_roots=server_private_roots(settings),
        )
    )


def _service(
    settings: Settings = Depends(get_settings),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> AgentTaskService:
    return get_agent_task_service(settings, event_bus)


def _encode_agentos_artifact_path(relpath: str) -> str:
    path = PurePosixPath(relpath)
    if path.is_absolute():
        raise HTTPException(404, "artifact not found")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(404, "artifact not found")
    return "/".join(quote(part, safe="") for part in parts)


@router.post(
    "/tasks",
    response_model=AgentTaskDTO,
    dependencies=[Depends(require_admin_access)],
)
async def create_task(
    body: AgentTaskCreateRequest,
    settings: Settings = Depends(get_settings),
    llm: LLMPort = Depends(get_llm),
    service: AgentTaskService = Depends(_service),
) -> AgentTaskDTO:
    if not body.text.strip():
        raise HTTPException(400, "text empty")
    # This is the authoritative API-side check. The embedded Claude Code
    # bridge receives a task only after the same strict V4 Flash plan selects
    # claude_code_runtime; there is intentionally no local runner fallback.
    decision = await LLMIntentRouter(settings, llm).route(
        body.text,
        available_context=[str(value) for value in body.context.values() if isinstance(value, str)],
    )
    plan = decision.params.get("intent_plan")
    if (
        decision.kind != "agent_task"
        or decision.params.get("ready_to_execute") is not True
        or not isinstance(plan, dict)
        or plan.get("execution_target") != "claude_code_runtime"
    ):
        raise HTTPException(status_code=409, detail="task requires an authorized intent plan")
    intent = AgentIntent(
        text=body.text,
        device_id=body.device_id,
        conversation_id=body.conversation_id,
        message_id=body.message_id,
        title=body.title,
        task_kind=body.task_kind or "agent_task",
        context=body.context,
        output_contract=body.output_contract,
        timeout_s=body.timeout_s or settings.agent_task_timeout_s,
    )
    rec = await service.submit_task(intent)
    return _task_dto(rec, settings)


@router.get("/tasks", response_model=list[AgentTaskDTO])
async def list_tasks(
    device_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> list[AgentTaskDTO]:
    return [
        _task_dto(r, settings) for r in await service.list_tasks(device_id=device_id, limit=limit)
    ]


@router.get("/tasks/{task_id}", response_model=AgentTaskDTO)
async def get_task(
    task_id: str,
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> AgentTaskDTO:
    rec = await service.get_task(task_id)
    if rec is None:
        raise HTTPException(404, "task not found")
    return _task_dto(rec, settings)


@router.get("/tasks/{task_id}/events", response_model=AgentTaskEventsDTO)
async def list_task_events(
    task_id: str,
    after_seq: int = Query(0, ge=0),
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> AgentTaskEventsDTO:
    events, snapshot, last_seq = await service.list_events(task_id, after_seq=after_seq)
    if last_seq == 0 and not snapshot:
        raise HTTPException(404, "task not found")
    return _task_events_dto(
        task_id,
        events=[e.model_dump(mode="json") for e in events],
        snapshot=snapshot,
        last_seq=last_seq,
        settings=settings,
    )


@router.get("/tasks/{task_id}/artifacts/{relpath:path}")
async def proxy_task_artifact(
    task_id: str,
    relpath: str,
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> Response:
    rec = await service.get_task(task_id)
    if rec is None or not rec.runner_task_id:
        raise HTTPException(404, "artifact not found")
    encoded_relpath = _encode_agentos_artifact_path(relpath)
    runner_task_id = quote(rec.runner_task_id, safe="")
    upstream_url = (
        f"{service.backend.base_url}/api/v1/tasks/{runner_task_id}/artifacts/{encoded_relpath}"
    )
    client = httpx.AsyncClient(timeout=30.0, trust_env=False)
    upstream: httpx.Response | None = None
    try:
        request = client.build_request("GET", upstream_url)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPStatusError as exc:
        await _close_artifact_proxy(upstream, client)
        if exc.response.status_code == 404:
            raise HTTPException(404, "artifact not found") from exc
        raise HTTPException(502, "artifact proxy failed") from exc
    except httpx.HTTPError as exc:
        await _close_artifact_proxy(upstream, client)
        raise HTTPException(502, "artifact proxy failed") from exc

    try:
        content_length = validated_content_length(
            upstream,
            max_bytes=settings.agent_artifact_proxy_max_bytes,
        )
    except ArtifactContentLengthError as exc:
        await _close_artifact_proxy(upstream, client)
        raise HTTPException(502, "artifact proxy returned invalid size") from exc
    except ArtifactSizeLimitError as exc:
        await _close_artifact_proxy(upstream, client)
        raise HTTPException(413, "artifact exceeds proxy size limit") from exc

    headers = dict(PRIVATE_NO_STORE_HEADERS)
    disposition = upstream.headers.get("content-disposition")
    if disposition:
        headers["content-disposition"] = disposition
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return StreamingResponse(
        _bounded_artifact_body(
            upstream,
            client,
            max_bytes=settings.agent_artifact_proxy_max_bytes,
            chunk_bytes=settings.upload_read_chunk_bytes,
        ),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=AgentTaskDTO,
    dependencies=[Depends(require_admin_access)],
)
async def cancel_task(
    task_id: str,
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> AgentTaskDTO:
    rec = await service.cancel_task(task_id)
    if rec is None:
        raise HTTPException(404, "task not found")
    return _task_dto(rec, settings)


@router.post(
    "/tasks/{task_id}/retry",
    response_model=AgentTaskDTO,
    dependencies=[Depends(require_admin_access)],
)
async def retry_task(
    task_id: str,
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> AgentTaskDTO:
    new_rec = await service.retry_task(task_id)
    if new_rec is None:
        raise HTTPException(404, "task not found")
    return _task_dto(new_rec, settings)


@router.get("/grants", response_model=GrantStatusDTO)
async def get_grant(
    device_id: str = Query(...),
    runner: str = Query(RUNNER_CLAUDE_CODE),
    service: AgentTaskService = Depends(_service),
) -> GrantStatusDTO:
    if runner != RUNNER_CLAUDE_CODE:
        return GrantStatusDTO(grant=None)
    grant = await service.get_active_grant(device_id=device_id, runner=runner)
    return GrantStatusDTO(grant=GrantDTO.from_grant(grant) if grant else None)


@router.post(
    "/grants/claude_code",
    response_model=GrantCreateResponse,
    dependencies=[Depends(require_admin_access)],
)
async def create_grant(
    body: GrantCreateRequest,
    service: AgentTaskService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> GrantCreateResponse:
    if body.permission_profile != PROFILE_FULL_ACCESS:
        raise HTTPException(400, "unsupported permission_profile")
    grant = await service.create_grant(
        device_id=body.device_id,
        workspace_ids=body.workspace_ids,
        permission_profile=body.permission_profile,
    )
    resumed_task = None
    if body.resume_task_id:
        try:
            resumed_task = _task_dto(
                await service.resume_with_grant(body.resume_task_id, grant),
                settings,
            )
        except KeyError as exc:
            raise HTTPException(404, "task not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, "grant device mismatch") from exc
    return GrantCreateResponse(grant=GrantDTO.from_grant(grant), resumed_task=resumed_task)


@router.delete("/grants/{grant_id}", dependencies=[Depends(require_admin_access)])
async def revoke_grant(
    grant_id: str,
    service: AgentTaskService = Depends(_service),
) -> dict[str, bool]:
    return {"ok": await service.revoke_grant(grant_id)}
