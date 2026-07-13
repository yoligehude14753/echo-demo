"""Owner-scoped L0-L3 memory HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.deps import get_llm_singleton
from app.config import Settings, get_settings
from app.memory import MemoryScope, MemoryService, get_memory_service
from app.memory.models import (
    ExtractionResult,
    MemoryKind,
    MemoryRecord,
    ProfileSettingRecord,
    ProvenanceRecord,
    RecallResult,
)
from app.ports.llm import LLMPort
from app.schemas.memory import (
    MemoryDeleteResponse,
    MemoryExplicitExtractRequest,
    MemoryNodeUpdateRequest,
    MemoryProfileWriteRequest,
    MemoryRecallRequest,
    WorkingMemoryClearResponse,
)
from app.security.context import current_principal

router = APIRouter(prefix="/memory", tags=["memory"])


def get_memory_dependency(
    settings: Settings = Depends(get_settings),
    llm: LLMPort = Depends(get_llm_singleton),
) -> MemoryService:
    return get_memory_service(settings, llm)


def _scope() -> MemoryScope:
    return MemoryScope.from_principal(current_principal())


@router.post("/recall", response_model=RecallResult)
async def recall_memory(
    body: MemoryRecallRequest,
    memory: MemoryService = Depends(get_memory_dependency),
) -> RecallResult:
    return await memory.recall(
        _scope(),
        body.text.strip(),
        conversation_id=body.conversation_id,
        limit=body.limit,
    )


@router.post("/extract", response_model=ExtractionResult)
async def extract_explicit_memory(
    body: MemoryExplicitExtractRequest,
    memory: MemoryService = Depends(get_memory_dependency),
) -> ExtractionResult:
    """Extract L2 candidates from a user-authored explicit memory statement.

    Clients cannot claim a meeting/artifact source; those trusted provenance
    records are created by internal ingestion paths only.
    """

    source_id = body.source_id or f"explicit_{uuid4().hex}"
    return await memory.extract_text(
        _scope(),
        text=body.text,
        source_kind="user_explicit",
        source_id=source_id,
        occurred_at=datetime.now(UTC),
        metadata={"confirmation": "user_explicit"},
    )


@router.get("/nodes", response_model=list[MemoryRecord])
async def list_memory_nodes(
    query: str | None = Query(default=None, max_length=2_000),
    kind: MemoryKind | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    memory: MemoryService = Depends(get_memory_dependency),
) -> list[MemoryRecord]:
    return await memory.list_nodes(
        _scope(),
        query=query,
        kind=kind,
        include_deleted=include_deleted,
        limit=limit,
    )


@router.get("/nodes/{memory_id}", response_model=MemoryRecord)
async def get_memory_node(
    memory_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> MemoryRecord:
    record = await memory.get_node(_scope(), memory_id)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return record


@router.get("/nodes/{memory_id}/provenance", response_model=list[ProvenanceRecord])
async def get_memory_provenance(
    memory_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> list[ProvenanceRecord]:
    if await memory.get_node(_scope(), memory_id) is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return await memory.provenance(_scope(), memory_id)


@router.post("/nodes/{memory_id}/confirm", response_model=MemoryRecord)
async def confirm_memory_node(
    memory_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> MemoryRecord:
    record = await memory.confirm_node(_scope(), memory_id)
    if record is None:
        raise HTTPException(status_code=404, detail="active memory not found")
    return record


@router.patch("/nodes/{memory_id}", response_model=MemoryRecord)
async def update_memory_node(
    body: MemoryNodeUpdateRequest,
    memory_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> MemoryRecord:
    record = await memory.update_node(
        _scope(),
        memory_id,
        content=body.content,
        kind=body.kind,
        canonical_key=body.canonical_key,
        salience=body.salience,
        memory_scope=body.scope,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="active memory not found")
    return record


@router.delete("/nodes/{memory_id}", response_model=MemoryDeleteResponse)
async def delete_memory_node(
    memory_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> MemoryDeleteResponse:
    deleted = await memory.delete_node(_scope(), memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryDeleteResponse(deleted=True)


@router.get("/profile", response_model=list[ProfileSettingRecord])
async def list_memory_profile(
    memory: MemoryService = Depends(get_memory_dependency),
) -> list[ProfileSettingRecord]:
    return await memory.profile(_scope())


@router.put("/profile/{config_key}", response_model=ProfileSettingRecord)
async def upsert_memory_profile(
    body: MemoryProfileWriteRequest,
    config_key: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> ProfileSettingRecord:
    return await memory.upsert_profile_setting(
        _scope(),
        config_key,
        body.value,
        description=body.description,
    )


@router.delete("/profile/{config_key}", response_model=MemoryDeleteResponse)
async def delete_memory_profile(
    config_key: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> MemoryDeleteResponse:
    deleted = await memory.delete_profile_setting(_scope(), config_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="profile setting not found")
    return MemoryDeleteResponse(deleted=True)


@router.delete(
    "/working/{conversation_id}",
    response_model=WorkingMemoryClearResponse,
)
async def clear_working_memory(
    conversation_id: str = Path(min_length=1, max_length=128),
    memory: MemoryService = Depends(get_memory_dependency),
) -> WorkingMemoryClearResponse:
    return WorkingMemoryClearResponse(cleared=await memory.clear_working(_scope(), conversation_id))


__all__ = ["get_memory_dependency", "router"]
