"""产物生成（PPT / Word / Excel / HTML / Markdown / PDF / TXT）schema。

工具链选型（2026-05-26 用户决策 PR-12，2026-05-28 扩展 PR-phase4-m3）：
- ppt/pptx → pptxgenjs（Node.js）
- word     → python-docx
- xlsx/excel → openpyxl + LibreOffice headless
- html     → single-file tailwind CDN
- markdown → LLM 直出 GFM 文本，直接落盘
- pdf      → fpdf2 + Noto Sans SC TTF（中文）
- txt      → LLM 直出纯文本，直接落盘
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 对外暴露的所有写法（含别名）
ArtifactKind = Literal[
    "ppt",
    "pptx",
    "word",
    "docx",
    "xlsx",
    "excel",
    "html",
    "markdown",
    "md",
    "mdown",
    "pdf",
    "txt",
    "text",
]

# 规范化后的 7 种核心类型（adapter 内部统一用这个）
CanonicalKind = Literal["pptx", "word", "xlsx", "html", "markdown", "pdf", "txt"]

# 别名映射：外部写法 → 规范化写法
_KIND_NORMALIZE: dict[str, str] = {
    "ppt": "pptx",
    "pptx": "pptx",
    "word": "word",
    "docx": "word",
    "xlsx": "xlsx",
    "excel": "xlsx",
    "html": "html",
    "markdown": "markdown",
    "md": "markdown",
    "mdown": "markdown",
    "pdf": "pdf",
    "txt": "txt",
    "text": "txt",
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
    # M_minutes_refactor：当指令是为了完成某条会议待办（todo），可选带这两
    # 个字段；artifact 生成成功后会回写 meetings.minutes_json.todos[id]
    # → status="done" + artifact_id，并发 ``meeting.todo.completed`` 事件。
    meeting_id: str | None = None
    todo_id: str | None = None


class GeneratedArtifact(BaseModel):
    """产物生成结果。

    ``title`` 字段在 P4-M3 引入：从 brief 提炼前 ~40 字，方便前端列表展示，
    并参与 download endpoint 的文件名生成。旧 fixture 不传该字段时默认为 ""。
    """

    artifact_id: str
    artifact_type: str  # canonical kind
    title: str = ""
    file_path: str
    mime_type: str
    size_bytes: int
    generation_latency_ms: float
    model: str
    metadata: dict[str, str] = Field(default_factory=dict)


# 旧别名，避免下游引用断
ArtifactResult = GeneratedArtifact
