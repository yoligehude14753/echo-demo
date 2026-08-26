"""Workflow 0.3 REST API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.agents.service import AgentTaskService, get_agent_task_service
from app.api.deps import get_event_bus, get_workflow_dispatcher, get_workflow_service
from app.config import Settings, get_settings
from app.schemas.workflow import (
    WorkflowCancelRequest,
    WorkflowEventDTO,
    WorkflowEventsResponse,
    WorkflowRetryRequest,
    WorkflowRunDTO,
)
from app.security.context import current_principal
from app.security.public_projection import project_client_dict, server_private_roots
from app.workflows.kernel import WorkflowDispatcher
from app.workflows.service import (
    InvalidWorkflowTransition,
    WorkflowConflictError,
    WorkflowService,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _run_dto(run: WorkflowRunDTO, settings: Settings) -> WorkflowRunDTO:
    return WorkflowRunDTO.model_validate(
        project_client_dict(
            run.model_dump(mode="json"),
            current_principal(),
            private_roots=server_private_roots(settings),
        )
    )


def _event_dto(event: WorkflowEventDTO, settings: Settings) -> WorkflowEventDTO:
    return WorkflowEventDTO.model_validate(
        project_client_dict(
            event.model_dump(mode="json"),
            current_principal(),
            private_roots=server_private_roots(settings),
        )
    )


def _agent_service(
    settings: Settings = Depends(get_settings),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> AgentTaskService:
    return get_agent_task_service(settings, event_bus)


@router.get("/runs", response_model=list[WorkflowRunDTO])
async def list_runs(
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    meeting_id: str | None = Query(None),
    todo_id: str | None = Query(None),
    agent_task_id: str | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    settings: Settings = Depends(get_settings),
) -> list[WorkflowRunDTO]:
    rows = await service.list_runs(
        meeting_id=meeting_id,
        todo_id=todo_id,
        agent_task_id=agent_task_id,
        state=state,
        limit=limit,
    )
    return [_run_dto(row.to_dto(), settings) for row in rows]


@router.get("/runs/{run_id}", response_model=WorkflowRunDTO)
async def get_run(
    run_id: str,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    settings: Settings = Depends(get_settings),
) -> WorkflowRunDTO:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return _run_dto(run.to_dto(), settings)


@router.get("/runs/{run_id}/events", response_model=WorkflowEventsResponse)
async def get_run_events(
    run_id: str,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    after_seq: int = Query(0, ge=0),
    settings: Settings = Depends(get_settings),
) -> WorkflowEventsResponse:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    events = await service.list_events(run_id, after_seq=after_seq)
    return WorkflowEventsResponse(
        run_id=run_id,
        events=[_event_dto(event.to_dto(), settings) for event in events],
        snapshot=_run_dto(run.to_dto(), settings),
    )


@router.post("/runs/{run_id}/cancel", response_model=WorkflowRunDTO)
async def cancel_run(
    run_id: str,
    body: WorkflowCancelRequest,
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    agents: Annotated[AgentTaskService, Depends(_agent_service)],
    settings: Settings = Depends(get_settings),
) -> WorkflowRunDTO:
    existing = await service.get_run(run_id)
    if existing is None:
        raise HTTPException(404, "workflow run not found")
    if existing.kind == "agent_task" and existing.agent_task_id:
        await agents.cancel_task(existing.agent_task_id)
        projected = await service.get_run(run_id)
        if projected is None:
            raise HTTPException(404, "workflow run not found")
        return _run_dto(projected.to_dto(), settings)
    run = await dispatcher.cancel(run_id, reason=body.reason)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return _run_dto(run.to_dto(), settings)


@router.post("/runs/{run_id}/retry", response_model=WorkflowRunDTO)
async def retry_run(
    run_id: str,
    body: WorkflowRetryRequest,
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    agents: Annotated[AgentTaskService, Depends(_agent_service)],
    settings: Settings = Depends(get_settings),
) -> WorkflowRunDTO:
    existing = await service.get_run(run_id)
    if existing is None:
        raise HTTPException(404, "workflow run not found")
    if existing.kind == "agent_task" and existing.agent_task_id:
        retried = await agents.retry_task(existing.agent_task_id)
        if retried is None or retried.workflow_run_id is None:
            raise HTTPException(409, "agent task cannot be retried")
        projected = await service.get_run(retried.workflow_run_id)
        if projected is None:
            raise HTTPException(500, "agent retry workflow missing")
        return _run_dto(projected.to_dto(), settings)
    try:
        run = await dispatcher.retry(run_id, reason=body.reason)
    except (InvalidWorkflowTransition, WorkflowConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return _run_dto(run.to_dto(), settings)
