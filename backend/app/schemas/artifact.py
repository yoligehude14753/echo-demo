"""产物生成（PPT / Word / Excel / HTML）schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ArtifactKind = Literal["ppt", "pptx", "word", "xlsx", "excel", "html"]


class ArtifactRequest(BaseModel):
    artifact_type: ArtifactKind
    title: str
    brief: str  # 自然语言指令
    extra_instructions: str | None = None
    context_refs: list[str] = Field(default_factory=list)  # 关联会议/RAG/Web
    quality_first: bool = True  # 质量优先 → max_tokens=80000


class GeneratedArtifact(BaseModel):
    """产物生成结果。"""

    artifact_id: str
    artifact_type: str
    file_path: str
    mime_type: str
    size_bytes: int
    generation_latency_ms: float
    model: str
    metadata: dict[str, str] = Field(default_factory=dict)


# 旧别名，避免下游引用断
ArtifactResult = GeneratedArtifact
