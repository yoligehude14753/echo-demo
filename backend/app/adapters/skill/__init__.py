"""Skill 执行器：LLM 生成代码 → 执行 → 产物（Word/Excel/HTML）。"""

from app.adapters.skill.llm_skill import SkillError, SkillExecutor

__all__ = ["SkillError", "SkillExecutor"]
