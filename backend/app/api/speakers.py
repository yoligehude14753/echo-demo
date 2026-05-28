"""说话人 API：列出已知说话人 + 改名。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_diarizer_singleton, get_repository, get_speaker_registry
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort, SpeakerProfileRecord
from app.use_cases.speaker_registry import SpeakerRegistry

router = APIRouter(prefix="/speakers", tags=["speakers"])


class SpeakerRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)


class SpeakerInfo(BaseModel):
    speaker_id: str
    label: str | None = None
    n_samples: int = 0
    first_seen_at: str
    last_seen_at: str


def _to_info(r: SpeakerProfileRecord) -> SpeakerInfo:
    return SpeakerInfo(
        speaker_id=r.speaker_id,
        label=r.label,
        n_samples=r.n_samples,
        first_seen_at=r.first_seen_at.isoformat(),
        last_seen_at=r.last_seen_at.isoformat(),
    )


@router.get("", response_model=list[SpeakerInfo])
async def list_speakers(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[SpeakerInfo]:
    rows = await repository.list_speakers()
    return [_to_info(r) for r in rows]


@router.post("/{speaker_id}/rename", response_model=SpeakerInfo)
async def rename_speaker(
    speaker_id: str,
    body: Annotated[SpeakerRenameRequest, Body(...)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    registry: Annotated[SpeakerRegistry, Depends(get_speaker_registry)],
    diarizer: Annotated[DiarizerPort, Depends(get_diarizer_singleton)],
) -> SpeakerInfo:
    """改 speaker 名 + 把当前声纹写盘，让下次进程启动 hydrate 时能匹配回原 ID。

    用户 2026-05-28 期望：改过名的人下次同样声纹再说话要自动改过来。所以
    rename 时同时调 ``diarizer.persist_profile_for_user_label`` 把 embedding
    落 repo（ECAPA 默认 persist=False 不写盘，但 user-labeled speaker 例外）。
    """
    await registry.rename(speaker_id, body.label)
    if hasattr(diarizer, "persist_profile_for_user_label"):
        await diarizer.persist_profile_for_user_label(speaker_id)
    row = await repository.get_speaker(speaker_id)
    if row is None:
        # 改名后理应存在；防御性 fallback
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return SpeakerInfo(
            speaker_id=speaker_id,
            label=body.label,
            n_samples=0,
            first_seen_at=now.isoformat(),
            last_seen_at=now.isoformat(),
        )
    return _to_info(row)
