"""minutes_kit — 离线会议纪要产物精修工具。

这是 echo 仓库内的独立子模块，与 backend/app/meeting/ 实时摘要 Agent 无关。
本模块不 import 任何 backend.app.* 模块，也不被它们 import；只通过 LLMClient 注入完成依赖反转。

公开 API:
    generate_minutes(transcript, llm_client, out_dir, **kwargs) -> MinutesResult

入口示例:
    from minutes_kit import generate_minutes
    from minutes_kit.llm_client import OpenAIClient

    result = await generate_minutes(
        transcript=transcript_lines,
        llm_client=OpenAIClient(),
        out_dir=Path("./out/run_001/"),
        participants=["A", "B", "C"],
        title_hint="周三例会",
    )
    # result.preview_html_path / result.docx_path / result.data
"""
from __future__ import annotations

from minutes_kit.models import (
    Decision,
    MeetingMinutesData,
    MinutesResult,
    Todo,
    Topic,
    TranscriptTurn,
)

__all__ = [
    "Decision",
    "MeetingMinutesData",
    "MinutesResult",
    "Todo",
    "Topic",
    "TranscriptTurn",
    "generate_minutes",
]


async def generate_minutes(*args, **kwargs) -> MinutesResult:
    """门面函数：延迟 import orchestrator，避免 cli/demo 启动时 eager 拉重依赖。"""
    from minutes_kit.orchestrator import generate_minutes as _impl

    return await _impl(*args, **kwargs)
