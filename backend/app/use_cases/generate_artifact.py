"""use_case: generate_artifact — 触发 SkillExecutor 生成产物。

注意 architecture fitness function：use_case 只能依赖 ports + schemas，
adapter 实例由 api 层注入。这里通过 SkillRunner 协议（duck-typed）调用。
"""

from __future__ import annotations

from typing import Protocol

from app.ports.llm import LLMPort
from app.schemas.artifact import GeneratedArtifact


class SkillRunner(Protocol):
    async def generate(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact: ...


async def generate_artifact(
    *,
    runner: SkillRunner,
    llm: LLMPort,
    artifact_type: str,
    brief: str,
    extra_instructions: str | None = None,
) -> GeneratedArtifact:
    return await runner.generate(
        llm=llm,
        artifact_type=artifact_type,
        brief=brief,
        extra_instructions=extra_instructions,
    )
