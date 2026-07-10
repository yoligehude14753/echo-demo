"""Workflow 0.3 REST API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_workflow_service
from app.schemas.workflow import (
    WorkflowCancelRequest,
    WorkflowEventsResponse,
    WorkflowRetryRequest,
    WorkflowRunDTO,
)
from app.workflows.service import WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.get("/runs", response_model=list[WorkflowRunDTO])
async def list_runs(
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    meeting_id: str | None = Query(None),
    todo_id: str | None = Query(None),
    agent_task_id: str | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[WorkflowRunDTO]:
    rows = await service.list_runs(
        meeting_id=meeting_id,
        todo_id=todo_id,
        agent_task_id=agent_task_id,
        state=state,
        limit=limit,
    )
    return [row.to_dto() for row in rows]


@router.get("/runs/{run_id}", response_model=WorkflowRunDTO)
async def get_run(
    run_id: str,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowRunDTO:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return run.to_dto()


@router.get("/runs/{run_id}/events", response_model=WorkflowEventsResponse)
async def get_run_events(
    run_id: str,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    after_seq: int = Query(0, ge=0),
) -> WorkflowEventsResponse:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    events = await service.list_events(run_id, after_seq=after_seq)
    return WorkflowEventsResponse(
        run_id=run_id,
        events=[event.to_dto() for event in events],
        snapshot=run.to_dto(),
    )


@router.post("/runs/{run_id}/cancel", response_model=WorkflowRunDTO)
async def cancel_run(
    run_id: str,
    body: WorkflowCancelRequest,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowRunDTO:
    run = await service.request_cancel(run_id, reason=body.reason)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return run.to_dto()


@router.post("/runs/{run_id}/retry", response_model=WorkflowRunDTO)
async def retry_run(
    run_id: str,
    body: WorkflowRetryRequest,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowRunDTO:
    run = await service.retry_run(run_id, reason=body.reason)
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return run.to_dto()
