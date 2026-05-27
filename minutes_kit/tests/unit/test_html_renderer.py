"""HTML 渲染器测试。"""
from __future__ import annotations

from pathlib import Path

from minutes_kit.renderers.html import _markdown_to_html, render_html


def test_render_html_produces_complete_doc(sample_minutes_data, tmp_path: Path):
    out = tmp_path / "preview.html"
    render_html(sample_minutes_data, out, inline_mermaid_js=False)
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    # 完整 doc 结构
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    # 标题渲染
    assert "周三例会" in html
    # 决议 + 待办都出现
    assert "Word 模板由 B 负责" in html  # 来自 fixture（无 markdown 加粗的简单 decision）
    assert "出 Word 模板" in html
    # 待办表格存在
    assert '<table class="doc-table">' in html
    assert "<th>任务</th>" in html
    # 流程图嵌入
    assert 'class="mermaid"' in html
    assert "flowchart TD" in html
    # 章节标题（不再用 emoji）
    assert "会议决议" in html
    assert "待办事项" in html
    assert "会议流程" in html
    # 核心结论 callout
    assert "核心结论" in html
    assert "callout" in html
    # CSS 暗色模式
    assert "prefers-color-scheme: dark" in html
    # AI 标签
    assert "AI 生成" in html
    # 决议三栏卡片装饰
    assert 'class="card ' in html
    assert "tint-orange" in html or "tint-blue" in html or "tint-green" in html
    # AI 生成是低调右下角小字
    assert 'class="ai-note"' in html or "AI 生成" in html


def test_render_html_inline_mermaid_loader_fallback(sample_minutes_data, tmp_path: Path):
    """static/mermaid.min.js 不存在时应自动用 CDN loader。"""
    out = tmp_path / "preview.html"
    render_html(sample_minutes_data, out, inline_mermaid_js=True)
    html = out.read_text(encoding="utf-8")
    # 没装 mermaid.min.js 时走 cdn loader
    assert "cdn.jsdelivr.net/npm/mermaid" in html or "window.mermaid" in html


def test_render_html_without_flow_mermaid_skips_section(sample_minutes_data, tmp_path: Path):
    sample_minutes_data.flow_mermaid = ""
    out = tmp_path / "preview.html"
    render_html(sample_minutes_data, out, inline_mermaid_js=False)
    html = out.read_text(encoding="utf-8")
    # 不渲染会议流程章节标题（h2.section-title 不会出现"会议流程"）
    assert ">会议流程<" not in html
    # 没流程图就不需要 mermaid script
    assert 'class="mermaid"' not in html


def test_render_html_empty_decisions_shows_hint(sample_minutes_data, tmp_path: Path):
    sample_minutes_data.decisions = []
    sample_minutes_data.todos = []
    out = tmp_path / "preview.html"
    render_html(sample_minutes_data, out, inline_mermaid_js=False)
    html = out.read_text(encoding="utf-8")
    assert "本次会议未产生明确决议" in html
    assert "本次会议未产生待办事项" in html


def test_render_html_escapes_xss(sample_minutes_data, tmp_path: Path):
    """决议字段被 HTML escape，不可注入 script。"""
    sample_minutes_data.decisions[0].statement = "<script>alert(1)</script>恶意"
    out = tmp_path / "preview.html"
    render_html(sample_minutes_data, out, inline_mermaid_js=False)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>恶意" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;恶意" in html


# ── _markdown_to_html ─────────────────────────────────────────────────


def test_md_headings():
    out = _markdown_to_html("# H1\n## H2\n### H3")
    assert "<h1>H1</h1>" in out
    assert "<h2>H2</h2>" in out
    assert "<h3>H3</h3>" in out


def test_md_unordered_list():
    out = _markdown_to_html("- a\n- b\n- c")
    assert out.count("<ul>") == 1
    assert out.count("</ul>") == 1
    assert out.count("<li>") == 3


def test_md_ordered_list():
    out = _markdown_to_html("1. first\n2. second\n3. third")
    assert "<ol>" in out
    assert "<li>first</li>" in out


def test_md_mixed_list_separation():
    """ul 和 ol 之间应正确切换闭合。"""
    out = _markdown_to_html("- a\n- b\n\n1. first\n2. second")
    assert out.index("</ul>") < out.index("<ol>")


def test_md_paragraph_breaks_on_blank_line():
    out = _markdown_to_html("para one\n\npara two")
    assert out.count("<p>") == 2


def test_md_inline_bold_and_code():
    out = _markdown_to_html("**important** and `code`")
    assert "<strong>important</strong>" in out
    assert "<code>code</code>" in out


def test_md_escapes_html():
    out = _markdown_to_html("# <script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_md_empty_returns_empty():
    assert _markdown_to_html("") == ""
    assert _markdown_to_html("   ") == ""
