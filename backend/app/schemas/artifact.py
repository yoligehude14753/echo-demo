"""产物生成（PPT / Word / Excel / HTML）schema。

工具链选型（2026-05-26 用户决策 PR-12）：
- ppt/pptx → pptxgenjs（Node.js）
- word     → python-docx
- xlsx/excel → openpyxl + LibreOffice headless
- html     → single-file tailwind CDN
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 对外暴露的所有写法（含别名）
ArtifactKind = Literal["ppt", "pptx", "word", "xlsx", "excel", "html"]

# 规范化后的 4 种核心类型（adapter 内部统一用这个）
CanonicalKind = Literal["pptx", "word", "xlsx", "html"]

# 别名映射：外部写法 → 规范化写法
_KIND_NORMALIZE: dict[str, str] = {
    "ppt": "pptx",
    "pptx": "pptx",
    "word": "word",
    "xlsx": "xlsx",
    "excel": "xlsx",
    "html": "html",
}

SUPPORTED_KINDS: frozenset[str] = frozenset(_KIND_NORMALIZE.keys())


def normalize_kind(kind: str) -> str:
    """把外部别名归一为 canonical kind；非法返回 ''。"""
    return _KIND_NORMALIZE.get(kind.lower().strip(), "")


class ArtifactRequest(BaseModel):
    """API 入口请求体，artifact_type 走 ArtifactKind 校验。"""

    artifact_type: ArtifactKind
    brief: str
    extra_instructions: str | None = None
    title: str | None = None
    context_refs: list[str] = Field(default_factory=list)  # 关联会议/RAG/Web
    quality_first: bool = True


class GeneratedArtifact(BaseModel):
    """产物生成结果。"""

    artifact_id: str
    artifact_type: str  # canonical kind
    file_path: str
    mime_type: str
    size_bytes: int
    generation_latency_ms: float
    model: str
    metadata: dict[str, str] = Field(default_factory=dict)


# 旧别名，避免下游引用断
ArtifactResult = GeneratedArtifact
