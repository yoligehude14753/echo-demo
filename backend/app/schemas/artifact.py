"""产物生成（PPT / Word / Excel / HTML）schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ArtifactKind = Literal["ppt", "word", "xlsx", "html"]


class ArtifactRequest(BaseModel):
    kind: ArtifactKind
    title: str
    instruction: str  # 自然语言指令
    context_refs: list[str] = Field(default_factory=list)  # 关联会议/RAG/Web
    quality_first: bool = True  # 是否质量优先（max_tokens=80000）


class ArtifactResult(BaseModel):
    kind: ArtifactKind
    path: str  # 本地路径
    bytes_size: int
    model_used: str
    generation_ms: int
    fix_loop_rounds: int = 0  # 触发了几次 iterative fix
    ok: bool = True
    error: str | None = None
