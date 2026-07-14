"""ASR-owned readiness projection endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.stt import get_asr_scheduler
from app.adapters.stt.contracts import ASRReadinessPublic
from app.adapters.stt.scheduler import ASRScheduler
from app.config import Settings, get_settings

router = APIRouter(prefix="/asr", tags=["asr"])


def _get_scheduler(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ASRScheduler:
    return get_asr_scheduler(settings)


@router.get("/readiness", response_model=ASRReadinessPublic)
async def get_asr_readiness(
    settings: Settings = Depends(get_settings),
    scheduler: ASRScheduler = Depends(_get_scheduler),
) -> ASRReadinessPublic:
    """Return cached scheduler/provider readiness without doing transcription."""

    return scheduler.readiness().to_public(ttl_s=settings.asr_readiness_stale_after_s)


__all__ = ["get_asr_readiness", "router"]
