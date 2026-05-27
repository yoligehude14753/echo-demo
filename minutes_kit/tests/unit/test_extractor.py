"""extractor 测试：用 mock LLM 跑 3 节点编排。"""
from __future__ import annotations

import pytest

from minutes_kit.extractor import ExtractorError, _sanitize_mermaid, extract_minutes
from minutes_kit.models import TranscriptTurn


def _make_turns() -> list[TranscriptTurn]:
    return [
        TranscriptTurn(speaker="A", text="今天对一下产物自动化", ts="10:00:00"),
        TranscriptTurn(speaker="B", text="Word 我来", ts="10:00:10"),
        TranscriptTurn(speaker="C", text="Excel 我来", ts="10:00:20"),
    ]


async def test_extract_minutes_happy_path(mock_llm):
    data = await extract_minutes(_make_turns(), llm_client=mock_llm)

    assert data.title == "周三例会"
    assert data.from_time == "10:00:00"
    assert data.to_time == "10:00:20"
    assert data.participants == ["A", "B", "C"]
    assert len(data.decisions) == 4
    assert len(data.todos) == 4
    assert data.todos[0].priority == "high"
    assert len(data.topics) == 3
    assert data.flow_kind == "flowchart"
    assert data.flow_mermaid.startswith("flowchart TD")
    assert data.minutes_id


async def test_extract_minutes_empty_transcript_rejected(mock_llm):
    with pytest.raises(ExtractorError, match="transcript 为空"):
        await extract_minutes([], llm_client=mock_llm)


async def test_extract_minutes_node_a_failure_aborts(mock_llm):
    mock_llm.fail_node = "A"
    with pytest.raises(Exception):  # 当前 Node A 异常直接冒泡（不是 ExtractorError）
        await extract_minutes(_make_turns(), llm_client=mock_llm)


async def test_extract_minutes_node_b_failure_aborts(mock_llm):
    mock_llm.fail_node = "B"
    with pytest.raises(ExtractorError, match="Node B 失败"):
        await extract_minutes(_make_turns(), llm_client=mock_llm)


async def test_extract_minutes_node_c_failure_uses_placeholder(mock_llm, caplog):
    """Node C 失败不算硬故障，用占位流程图继续。"""
    mock_llm.fail_node = "C"
    data = await extract_minutes(_make_turns(), llm_client=mock_llm)
    assert data.flow_mermaid.startswith("flowchart TD")
    assert "会议开始" in data.flow_mermaid
    assert len(data.decisions) > 0  # B 仍跑出来了


async def test_explicit_participants_overrides_inferred(mock_llm):
    data = await extract_minutes(
        _make_turns(),
        llm_client=mock_llm,
        participants=["显式参会人"],
    )
    assert data.participants == ["显式参会人"]


async def test_title_hint_passed_through_in_prompt(mock_llm):
    await extract_minutes(_make_turns(), llm_client=mock_llm, title_hint="自定义标题")
    # 验证 user message 含 hint
    node_a_call = next(c for c in mock_llm.calls if c["kind"] == "schema")
    user_msg = next(m["content"] for m in node_a_call["messages"] if m["role"] == "user")
    assert "自定义标题" in user_msg


# ── sanitize_mermaid 单测 ─────────────────────────────────────────────


def test_sanitize_mermaid_strips_code_fence():
    src = "```mermaid\nflowchart TD\n  a --> b\n```"
    out = _sanitize_mermaid(src, "flowchart")
    assert out.startswith("flowchart TD")
    assert "```" not in out


def test_sanitize_mermaid_strips_style_lines():
    src = "flowchart TD\n  a[A]\n  style a fill:#fff\n  a --> b\n  click a callback"
    out = _sanitize_mermaid(src, "flowchart")
    assert "style a" not in out
    assert "click a" not in out
    assert "a --> b" in out


def test_sanitize_mermaid_strips_subgraph():
    src = "flowchart TD\n  subgraph foo\n  a[A]\n  end\n  a --> b"
    out = _sanitize_mermaid(src, "flowchart")
    assert "subgraph" not in out
    assert "end" not in out.split("\n")
    assert "a[A]" in out
    assert "a --> b" in out


def test_sanitize_mermaid_rejects_invalid_first_line():
    with pytest.raises(ExtractorError, match="首行非法"):
        _sanitize_mermaid("graph TB\n  a --> b", "flowchart")  # graph != flowchart 也禁


def test_sanitize_mermaid_accepts_all_four_kinds():
    for first in ["flowchart TD", "flowchart LR", "sequenceDiagram", "mindmap", "timeline"]:
        src = f"{first}\n  some content"
        out = _sanitize_mermaid(src, "any")
        assert out.startswith(first)


def test_sanitize_mermaid_truncates_overly_long():
    src = "flowchart TD\n" + ("  a --> b\n" * 1000)
    out = _sanitize_mermaid(src, "flowchart")
    assert len(out) <= 4000


def test_sanitize_mermaid_rejects_empty():
    with pytest.raises(ExtractorError, match="为空"):
        _sanitize_mermaid("", "flowchart")
