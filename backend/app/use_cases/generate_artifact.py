"""use_case: generate_artifact — 触发 SkillExecutor 生成产物。

依赖 ports.SkillExecutorPort（架构 fitness 约束：use_case 只看 ports + schemas）。
"""

from __future__ import annotations

from app.ports.llm import LLMPort
from app.ports.skill import SkillExecutorPort
from app.schemas.artifact import GeneratedArtifact


async def generate_artifact(
    *,
    runner: SkillExecutorPort,
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
