"""EchoDesk agent task API。

客户端只和 EchoDesk 后端交互，不直连 AgentOS；普通 UI 也不展示 provider 名称。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.agents.base import AgentIntent
from app.agents.service import (
    PROFILE_FULL_ACCESS,
    RUNNER_CLAUDE_CODE,
    AgentRunnerGrant,
    AgentTaskRecord,
    get_agent_task_service,
)
from app.api.deps import get_event_bus
from app.config import Settings, get_settings

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


def _service(
    settings: Settings = Depends(get_settings),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
):
    return get_agent_task_service(settings, event_bus)


def _encode_agentos_artifact_path(relpath: str) -> str:
    path = PurePosixPath(relpath)
    if path.is_absolute():
        raise HTTPException(404, "artifact not found")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(404, "artifact not found")
    return "/".join(quote(part, safe="") for part in parts)


@router.post("/tasks", response_model=AgentTaskDTO)
async def create_task(
    body: AgentTaskCreateRequest,
    settings: Settings = Depends(get_settings),
    service=Depends(_service),
) -> AgentTaskDTO:
    if not body.text.strip():
        raise HTTPException(400, "text empty")
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
    return AgentTaskDTO.from_record(rec)


@router.get("/tasks", response_model=list[AgentTaskDTO])
async def list_tasks(
    device_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    service=Depends(_service),
) -> list[AgentTaskDTO]:
    return [AgentTaskDTO.from_record(r) for r in await service.list_tasks(device_id=device_id, limit=limit)]


@router.get("/tasks/{task_id}", response_model=AgentTaskDTO)
async def get_task(task_id: str, service=Depends(_service)) -> AgentTaskDTO:
    rec = await service.get_task(task_id)
    if rec is None:
        raise HTTPException(404, "task not found")
    return AgentTaskDTO.from_record(rec)


@router.get("/tasks/{task_id}/events", response_model=AgentTaskEventsDTO)
async def list_task_events(
    task_id: str,
    after_seq: int = Query(0, ge=0),
    service=Depends(_service),
) -> AgentTaskEventsDTO:
    events, snapshot, last_seq = await service.list_events(task_id, after_seq=after_seq)
    if last_seq == 0 and not snapshot:
        raise HTTPException(404, "task not found")
    return AgentTaskEventsDTO(
        task_id=task_id,
        events=[e.model_dump(mode="json") for e in events],
        snapshot=snapshot,
        last_seq=last_seq,
    )


@router.get("/tasks/{task_id}/artifacts/{relpath:path}")
async def proxy_task_artifact(
    task_id: str,
    relpath: str,
    service=Depends(_service),
) -> Response:
    rec = await service.get_task(task_id)
    if rec is None or not rec.runner_task_id:
        raise HTTPException(404, "artifact not found")
    encoded_relpath = _encode_agentos_artifact_path(relpath)
    runner_task_id = quote(rec.runner_task_id, safe="")
    upstream_url = (
        f"{service.backend.base_url}/api/v1/tasks/{runner_task_id}/artifacts/{encoded_relpath}"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            upstream = await client.get(upstream_url)
            upstream.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "artifact not found") from exc
        raise HTTPException(502, "artifact proxy failed") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "artifact proxy failed") from exc

    headers: dict[str, str] = {}
    disposition = upstream.headers.get("content-disposition")
    if disposition:
        headers["content-disposition"] = disposition
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@router.post("/tasks/{task_id}/cancel", response_model=AgentTaskDTO)
async def cancel_task(task_id: str, service=Depends(_service)) -> AgentTaskDTO:
    rec = await service.cancel_task(task_id)
    if rec is None:
        raise HTTPException(404, "task not found")
    return AgentTaskDTO.from_record(rec)


@router.post("/tasks/{task_id}/retry", response_model=AgentTaskDTO)
async def retry_task(task_id: str, service=Depends(_service)) -> AgentTaskDTO:
    rec = await service.get_task(task_id)
    if rec is None:
        raise HTTPException(404, "task not found")
    intent = AgentIntent(
        text=rec.intent_text,
        device_id=rec.device_id,
        conversation_id=rec.conversation_id,
        message_id=rec.message_id,
        title=rec.title,
        task_kind=rec.task_kind,
        context=rec.envelope.get("context") if isinstance(rec.envelope.get("context"), dict) else {},
        output_contract=(
            rec.envelope.get("output_contract")
            if isinstance(rec.envelope.get("output_contract"), dict)
            else {}
        ),
        timeout_s=rec.timeout_s,
    )
    new_rec = await service.submit_task(intent)
    return AgentTaskDTO.from_record(new_rec)


@router.get("/grants", response_model=GrantStatusDTO)
async def get_grant(
    device_id: str = Query(...),
    runner: str = Query(RUNNER_CLAUDE_CODE),
    service=Depends(_service),
) -> GrantStatusDTO:
    if runner != RUNNER_CLAUDE_CODE:
        return GrantStatusDTO(grant=None)
    grant = await service.get_active_grant(device_id=device_id, runner=runner)
    return GrantStatusDTO(grant=GrantDTO.from_grant(grant) if grant else None)


@router.post("/grants/claude_code", response_model=GrantCreateResponse)
async def create_grant(body: GrantCreateRequest, service=Depends(_service)) -> GrantCreateResponse:
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
            resumed_task = AgentTaskDTO.from_record(
                await service.resume_with_grant(body.resume_task_id, grant)
            )
        except KeyError as exc:
            raise HTTPException(404, "task not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, "grant device mismatch") from exc
    return GrantCreateResponse(grant=GrantDTO.from_grant(grant), resumed_task=resumed_task)


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str, service=Depends(_service)) -> dict[str, bool]:
    return {"ok": await service.revoke_grant(grant_id)}
