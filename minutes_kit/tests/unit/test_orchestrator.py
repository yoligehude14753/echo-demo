"""orchestrator 端到端测试：用 mock LLM 跑完整 generate_minutes 流程。

这个测试很关键：它验证「3 节点 LLM → JSON → HTML → docx 兜底」整条链路无报错。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from minutes_kit.models import TranscriptTurn
from minutes_kit.orchestrator import MinutesGenerationError, generate_minutes


async def test_generate_minutes_happy_path(mock_llm, tmp_path: Path):
    transcript = [
        TranscriptTurn(speaker="A", text="我们对一下产物自动化", ts="10:00:00"),
        TranscriptTurn(speaker="B", text="Word 我来", ts="10:00:10"),
        TranscriptTurn(speaker="C", text="Excel 我来", ts="10:00:20"),
    ]
    result = await generate_minutes(
        transcript=transcript,
        llm_client=mock_llm,
        out_dir=tmp_path,
        participants=["A", "B", "C"],
        title_hint="周三例会",
        use_claude_skill=False,  # 测试环境直接走 fallback
        inline_mermaid_js=False,
    )

    # 数据
    assert result.data.title == "周三例会"
    assert len(result.data.decisions) == 4
    assert len(result.data.todos) == 4

    # 产物文件
    assert result.data_json_path.exists()
    assert result.preview_html_path.exists()
    assert result.docx_path is not None and result.docx_path.exists()
    assert result.docx_generator == "python_fallback"

    # 内容验证
    html = result.preview_html_path.read_text(encoding="utf-8")
    assert "周三例会" in html
    assert "<strong>Word 模板</strong>" in html  # markdown 加粗已转 strong
    assert "由 B 负责" in html


async def test_generate_minutes_accepts_dict_transcript(mock_llm, tmp_path: Path):
    """允许传 dict 列表，自动转 TranscriptTurn。"""
    result = await generate_minutes(
        transcript=[
            {"speaker": "A", "text": "讨论 X", "ts": "10:00:00"},
            {"speaker": "B", "text": "同意", "ts": "10:00:05"},
        ],
        llm_client=mock_llm,
        out_dir=tmp_path,
        use_claude_skill=False,
    )
    assert result.data.title == "周三例会"


async def test_generate_minutes_empty_transcript_raises(mock_llm, tmp_path: Path):
    with pytest.raises(MinutesGenerationError, match="为空"):
        await generate_minutes(
            transcript=[],
            llm_client=mock_llm,
            out_dir=tmp_path,
        )


async def test_generate_minutes_filters_empty_text(mock_llm, tmp_path: Path):
    """全是空文本应等价于空 transcript。"""
    with pytest.raises(MinutesGenerationError, match="为空"):
        await generate_minutes(
            transcript=[
                TranscriptTurn(speaker="A", text="", ts="10:00:00"),
                TranscriptTurn(speaker="B", text="   ", ts="10:00:10"),
            ],
            llm_client=mock_llm,
            out_dir=tmp_path,
        )


async def test_generate_minutes_writes_data_json_first(mock_llm, tmp_path: Path):
    """data.json 应该在所有产物写完后存在（早于 docx 完成不重要，关键是它一定写入了）。"""
    result = await generate_minutes(
        transcript=[TranscriptTurn(speaker="A", text="x", ts="10:00")],
        llm_client=mock_llm,
        out_dir=tmp_path,
        use_claude_skill=False,
    )
    assert result.data_json_path.exists()
    import json
    loaded = json.loads(result.data_json_path.read_text(encoding="utf-8"))
    assert loaded["title"] == "周三例会"
    assert "minutes_id" in loaded


async def test_generate_minutes_unique_ids(mock_llm, tmp_path: Path):
    """两次调用产生不同的 minutes_id。"""
    r1 = await generate_minutes(
        transcript=[TranscriptTurn(speaker="A", text="x", ts="10:00")],
        llm_client=mock_llm,
        out_dir=tmp_path / "a",
        use_claude_skill=False,
    )
    r2 = await generate_minutes(
        transcript=[TranscriptTurn(speaker="A", text="y", ts="10:00")],
        llm_client=mock_llm,
        out_dir=tmp_path / "b",
        use_claude_skill=False,
    )
    assert r1.data.minutes_id != r2.data.minutes_id
