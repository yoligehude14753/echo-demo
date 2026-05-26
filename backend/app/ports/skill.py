"""Skill 执行器 Port：把 LLM 生成的代码执行成 PPT / Word / Excel / HTML 产物。

复用 echo/experiments/2026-05-26_anthropic_skill_quality 的 v6.7.1 工具链：
- ppt:  pptxgenjs (Node.js)
- word: python-docx
- xlsx: openpyxl + LibreOffice headless 重算
- html: single-file tailwind CDN
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.artifact import ArtifactRequest, ArtifactResult


@runtime_checkable
class SkillExecutorPort(Protocol):
    async def render(self, request: ArtifactRequest) -> ArtifactResult: ...
