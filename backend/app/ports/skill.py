"""Skill 执行器 Port：把 LLM 生成的代码执行成 PPT / Word / Excel / HTML 产物。

工具链（与 schemas.artifact 同步）：
- pptx: pptxgenjs (Node.js)
- word: python-docx
- xlsx: openpyxl + LibreOffice headless
- html: single-file tailwind CDN
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.ports.llm import LLMPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.skill_progress import SkillProgress


@runtime_checkable
class SkillExecutorPort(Protocol):
    """Skill 执行器接口。

    实现方负责：
      1) 按 artifact_type 选系统提示词
      2) 调 llm 生成代码 / HTML
      3) 在沙箱 build_dir 内执行（python / node / 直接写文件）
      4) 把产物归一到 output.<ext> 并返回 ``GeneratedArtifact``

    artifact_type 接受所有外部别名（ppt/pptx/word/xlsx/excel/html），实现方负责归一化。
    """

    async def generate(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact: ...

    def generate_stream(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> AsyncIterator[SkillProgress]:
        """流式版生成：yield ``SkillProgress`` 进度事件。

        与 ``generate`` 等价的契约：成功路径以 ``stage="done"`` 携带 artifact 收尾；
        失败时 yield ``stage="error"`` 并 re-raise ``SkillError`` / ``LLMError``。
        """
        ...
