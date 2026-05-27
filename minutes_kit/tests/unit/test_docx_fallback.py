"""docx 兜底 + LLM client 工具测试。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from minutes_kit._fallback_office import fallback_docx, fallback_html_minimal
from minutes_kit.llm_client import _parse_json_lenient
from minutes_kit.models import MeetingMinutesData
from minutes_kit.renderers.docx import render_docx


def test_fallback_docx_generates_valid_zip(sample_minutes_data, tmp_path: Path):
    out = tmp_path / "minutes.docx"
    fallback_docx(
        target_path=out,
        title=sample_minutes_data.title,
        abstract=sample_minutes_data.abstract,
        summary_md=sample_minutes_data.summary_md,
        decisions=[d.to_dict() for d in sample_minutes_data.decisions],
        todos=[t.to_dict() for t in sample_minutes_data.todos],
        topics=[t.to_dict() for t in sample_minutes_data.topics],
        flow_png_path=None,
        participants=sample_minutes_data.participants,
        time_range="10:00 - 10:04",
    )
    assert out.exists()
    assert out.stat().st_size > 4096
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        body = zf.read("word/document.xml").decode("utf-8")
        assert "周三例会" in body
        assert "Word 模板由 B 负责" in body
        # 待办表格中的负责人
        assert "B" in body and "C" in body


def test_fallback_docx_handles_no_data(tmp_path: Path):
    """没有任何 decisions/todos/topics，至少要有标题。"""
    out = tmp_path / "minutes.docx"
    fallback_docx(target_path=out, title="空会议")
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        body = zf.read("word/document.xml").decode("utf-8")
        assert "空会议" in body


def test_fallback_html_minimal_xss_safe(tmp_path: Path):
    out = tmp_path / "preview.html"
    fallback_html_minimal(
        target_path=out,
        title="<script>x</script>",
        abstract="",
        summary_md="",
        decisions=[{"statement": "<img src=x onerror=y>"}],
        todos=[],
    )
    html = out.read_text(encoding="utf-8")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x onerror=y>" not in html


async def test_render_docx_skipping_claude_uses_fallback(sample_minutes_data, tmp_path: Path):
    """use_claude_skill=False 应直接走 python-docx fallback。"""
    out = tmp_path / "minutes.docx"
    result = await render_docx(sample_minutes_data, out, use_claude_skill=False)
    assert result.generator == "python_fallback"
    assert result.docx_path == out
    assert out.exists()


async def test_render_docx_claude_unavailable_auto_fallback(
    sample_minutes_data, tmp_path: Path
):
    """没装 claude binary 时应自动 fallback，不抛异常。"""
    out = tmp_path / "minutes.docx"
    # 依靠环境：CI / 测试机器多半没装 claude；即便装了，timeout 较长，本测试不强制
    result = await render_docx(
        sample_minutes_data,
        out,
        use_claude_skill=True,
        claude_timeout_s=5.0,  # 失败就跳到 fallback
    )
    # 不管 claude 是否成功，最终应该有 docx 产物
    assert result.docx_path == out
    assert out.exists()
    assert result.generator in ("claude", "python_fallback")


# ── llm_client._parse_json_lenient ──────────────────────────────────


def test_parse_json_lenient_plain():
    assert _parse_json_lenient('{"a": 1}') == {"a": 1}


def test_parse_json_lenient_with_code_fence():
    raw = "```json\n{\"k\": \"v\"}\n```"
    assert _parse_json_lenient(raw) == {"k": "v"}


def test_parse_json_lenient_with_garbage_around():
    raw = "Here is the JSON:\n{\"x\": 2}\nHope that helps."
    assert _parse_json_lenient(raw) == {"x": 2}


def test_parse_json_lenient_rejects_empty():
    with pytest.raises(ValueError):
        _parse_json_lenient("")


def test_parse_json_lenient_rejects_no_brace():
    with pytest.raises(ValueError):
        _parse_json_lenient("just text no json")


# ── render_html → docx 流程 sanity ───────────────────────────────────


async def test_full_render_pipeline_no_claude(sample_minutes_data, tmp_path: Path):
    """模拟「跳过 claude，从 data 一路生成 docx」走完不抛错。"""
    from minutes_kit.renderers.html import render_html
    sample_minutes_data.write_json(tmp_path / "data.json")
    render_html(sample_minutes_data, tmp_path / "preview.html", inline_mermaid_js=False)
    result = await render_docx(sample_minutes_data, tmp_path / "minutes.docx", use_claude_skill=False)
    assert (tmp_path / "data.json").exists()
    assert (tmp_path / "preview.html").exists()
    assert (tmp_path / "minutes.docx").exists()
    assert result.generator == "python_fallback"


# ── MeetingMinutesData round-trip via JSON file ──────────────────────


def test_data_json_round_trip(sample_minutes_data, tmp_path: Path):
    p = tmp_path / "data.json"
    sample_minutes_data.write_json(p)
    loaded = MeetingMinutesData.read_json(p)
    assert loaded.title == sample_minutes_data.title
    assert len(loaded.decisions) == len(sample_minutes_data.decisions)
    assert loaded.flow_kind == "flowchart"
