"""HTTP API: 产物生成 / 下载。

POST /artifacts/generate — body { artifact_type: 'word'|'xlsx'|'html', brief: str }
GET  /artifacts/{id}/download — 下载产物文件
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.skill import SkillError, SkillExecutor
from app.api.deps import get_event_bus
from app.api.deps import get_llm_singleton as get_llm
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.events import EchoEvent
from app.use_cases.generate_artifact import generate_artifact

router = APIRouter(tags=["artifacts"])


_skill_singleton: SkillExecutor | None = None


def get_skill(settings: Settings = Depends(get_settings)) -> SkillExecutor:
    global _skill_singleton  # noqa: PLW0603
    if _skill_singleton is None:
        _skill_singleton = SkillExecutor(settings)
    return _skill_singleton


def reset_skill_singleton() -> None:
    global _skill_singleton  # noqa: PLW0603
    _skill_singleton = None


class GenerateRequest(BaseModel):
    artifact_type: str
    brief: str
    extra_instructions: str | None = None


@router.post("/artifacts/generate", response_model=GeneratedArtifact)
async def generate(
    body: GenerateRequest,
    llm: LLMPort = Depends(get_llm),
    runner: SkillExecutor = Depends(get_skill),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> GeneratedArtifact:
    if not body.brief.strip():
        raise HTTPException(status_code=400, detail="brief empty")
    await event_bus.publish(
        EchoEvent(
            type="artifact.generating",
            payload={"artifact_type": body.artifact_type, "brief": body.brief[:200]},
        )
    )
    try:
        artifact = await generate_artifact(
            runner=runner,
            llm=llm,
            artifact_type=body.artifact_type,
            brief=body.brief,
            extra_instructions=body.extra_instructions,
        )
    except SkillError as e:
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                payload={"artifact_type": body.artifact_type, "error": str(e)[:300]},
            )
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    await event_bus.publish(
        EchoEvent(type="artifact.ready", payload=artifact.model_dump(mode="json"))
    )
    return artifact


@router.get("/artifacts/{artifact_id}/download")
async def download(
    artifact_id: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    build_dir = Path(settings.skill_executor_build_dir).expanduser() / artifact_id
    if not build_dir.exists():
        raise HTTPException(status_code=404, detail="artifact not found")
    candidates = list(build_dir.glob("output.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail="output file missing")
    f = candidates[0]
    return FileResponse(f, filename=f.name)
