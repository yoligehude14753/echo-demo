"""Skill 执行器真 LLM E2E（Yunwu M2.7）。

仅在 YUNWU_OPEN_KEY 配置且网络可达时跑，否则 skip。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.skill.llm_skill import SkillExecutor
from app.config import Settings


def _yunwu_alive() -> bool:
    if not os.getenv("YUNWU_OPEN_KEY"):
        return False
    try:
        with socket.create_connection(("yunwu.ai", 443), timeout=3):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _yunwu_alive(), reason="YUNWU_OPEN_KEY 未设置或网络不可达")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_skill_html_real_yunwu(tmp_path: Path) -> None:
    s = Settings(
        skill_executor_build_dir=tmp_path / "skill_build",
        skill_executor_timeout_s=120,
        skill_executor_max_tokens=8000,  # E2E 用小一点避免烧钱
    )
    llm = OpenAICompatibleLLM(s)
    skill = SkillExecutor(s)

    art = await skill.generate(
        llm=llm,
        artifact_type="html",
        brief=(
            "生成一份单文件 HTML 简报：英伟达 2020-2025 年营收快照（用占位真实数字即可），"
            "深色主题 + Tailwind CDN + 至少一个 inline SVG 柱状图。无需联网。"
        ),
    )

    assert art.artifact_type == "html"
    assert art.file_path.endswith(".html")
    assert art.size_bytes > 1500
    html = Path(art.file_path).read_text(encoding="utf-8")
    assert "<!DOCTYPE" in html.upper() or "<html" in html.lower()
    assert "tailwindcss" in html.lower() or "tailwind" in html.lower()
    await llm.aclose()
