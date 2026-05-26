"""Skill 执行器单测：mock LLM，验证 4 种产物的代码路径。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from app.adapters.skill import SkillError, SkillExecutor
from app.adapters.skill.llm_skill import _strip_code_fence
from app.adapters.skill.node_executor import _is_safe_node
from app.adapters.skill.python_executor import _is_safe_python
from app.config import Settings
from app.schemas.artifact import SUPPORTED_KINDS, normalize_kind
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages: list[ChatMessage] | None = None

    async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
        self.last_messages = list(messages)
        return LLMResponse(
            content=self.content,
            model="MiniMax-M2.7",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=12.0,
        )

    async def chat_stream(self, _messages: list[ChatMessage], **_: Any):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path,
        skill_executor_build_dir=tmp_path / "skill_build",
        skill_executor_timeout_s=30,
        skill_executor_max_tokens=80_000,
    )


@pytest.mark.unit
def test_strip_code_fence_python() -> None:
    code = _strip_code_fence("```python\nprint(1)\n```")
    assert code == "print(1)"


@pytest.mark.unit
def test_strip_code_fence_html() -> None:
    code = _strip_code_fence("```html\n<!DOCTYPE html>\n<html></html>\n```")
    assert code.startswith("<!DOCTYPE html>")
    assert code.endswith("<html></html>")


@pytest.mark.unit
def test_strip_code_fence_no_fence() -> None:
    code = _strip_code_fence("from docx import Document\ndoc = Document()")
    assert "from docx" in code


@pytest.mark.unit
def test_strip_code_fence_skips_leading_prose() -> None:
    """M2.7 thinking 残留：LLM 输出前导自然语言，跳到第一行代码。"""
    raw = (
        "We need to use openpyxl with multiple sheets and formulas.\n"
        "Plan: 4 sheets, 25 formulas.\n"
        "from openpyxl import Workbook\n"
        "wb = Workbook()\n"
    )
    assert _strip_code_fence(raw).startswith("from openpyxl")


@pytest.mark.unit
def test_strip_code_fence_skips_leading_prose_node() -> None:
    raw = (
        "I'll build a pptx using pptxgenjs.\n"
        "const PptxGenJS = require('pptxgenjs');\n"
        "const pres = new PptxGenJS();\n"
    )
    assert _strip_code_fence(raw).startswith("const PptxGenJS")


@pytest.mark.unit
def test_is_safe_python_rejects_network() -> None:
    ok, reason = _is_safe_python("import socket\ns = socket.socket()")
    assert not ok
    assert "socket" in reason


@pytest.mark.unit
def test_is_safe_python_accepts_docx() -> None:
    ok, _ = _is_safe_python("from docx import Document\ndoc = Document()\ndoc.save('output.docx')")
    assert ok


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unsupported_artifact_type_raises(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeLLM("...")
    with pytest.raises(SkillError, match="unsupported"):
        await skill.generate(llm=llm, artifact_type="pdf", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_html_generation_minimal(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    html_code = (
        "<!DOCTYPE html>\n<html>\n<head><script src='https://cdn.tailwindcss.com'></script></head>\n"
        + "<body class='bg-slate-900 text-white p-8'>\n"
        + '<h1 class="text-2xl">测试报告</h1>\n'
        + "<div>"
        + "x" * 2000
        + "</div>\n"
        + "</body></html>"
    )
    llm = FakeLLM(html_code)
    art = await skill.generate(llm=llm, artifact_type="html", brief="生成 demo HTML")
    assert art.artifact_type == "html"
    assert art.file_path.endswith(".html")
    assert Path(art.file_path).exists()
    assert art.size_bytes > 1500
    # 校验系统 prompt 是 HTML 的
    assert llm.last_messages is not None
    assert "Tailwind" in llm.last_messages[0].content


@pytest.mark.asyncio
@pytest.mark.unit
async def test_html_too_short_raises(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeLLM("<html>x</html>")
    with pytest.raises(SkillError, match="too short"):
        await skill.generate(llm=llm, artifact_type="html", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_html_no_doctype_raises(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeLLM("just plain text but long enough to pass length check " * 100)
    with pytest.raises(SkillError, match="DOCTYPE"):
        await skill.generate(llm=llm, artifact_type="html", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_word_generation_executes_python_docx(tmp_path: Path) -> None:
    """生成最小可运行的 docx 代码 → 真实执行 → 验证文件大小。"""
    pytest.importorskip("docx")
    skill = SkillExecutor(_settings(tmp_path))
    code = (
        "from docx import Document\n"
        "doc = Document()\n"
        "doc.add_heading('echo-demo skill test', level=1)\n"
        "doc.add_paragraph('一段中文内容，验证 python-docx 链路。')\n"
        "doc.save('output.docx')\n"
    )
    llm = FakeLLM(code)
    art = await skill.generate(llm=llm, artifact_type="word", brief="生成 demo word")
    assert art.artifact_type == "word"
    assert art.file_path.endswith(".docx")
    assert Path(art.file_path).stat().st_size > 1000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_xlsx_generation_executes_openpyxl(tmp_path: Path) -> None:
    pytest.importorskip("openpyxl")
    skill = SkillExecutor(_settings(tmp_path))
    code = (
        "from openpyxl import Workbook\n"
        "wb = Workbook()\n"
        "ws = wb.active\n"
        "ws.title = '假设'\n"
        "ws['A1'] = '增长率'\n"
        "ws['B1'] = 0.15\n"
        "ws2 = wb.create_sheet('预测')\n"
        "ws2['A1'] = '=假设!B1*100'\n"
        "wb.save('output.xlsx')\n"
    )
    llm = FakeLLM(code)
    art = await skill.generate(llm=llm, artifact_type="xlsx", brief="生成 demo excel")
    assert art.artifact_type == "xlsx"
    assert art.file_path.endswith(".xlsx")
    assert Path(art.file_path).stat().st_size > 1000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_python_with_forbidden_import_raises(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    code = "import socket\nsocket.socket()\n"
    llm = FakeLLM(code)
    with pytest.raises(SkillError, match="forbidden"):
        await skill.generate(llm=llm, artifact_type="word", brief="x")


# ── PR-12 新增：ArtifactKind 别名归一 + pptx 路径 ─────────────────────────


@pytest.mark.unit
def test_supported_kinds_covers_all_aliases() -> None:
    assert {"ppt", "pptx", "word", "xlsx", "excel", "html"} == SUPPORTED_KINDS


@pytest.mark.unit
def test_normalize_kind_pptx_alias() -> None:
    assert normalize_kind("ppt") == "pptx"
    assert normalize_kind("PPTX") == "pptx"
    assert normalize_kind("excel") == "xlsx"
    assert normalize_kind("xlsx") == "xlsx"
    assert normalize_kind("word") == "word"
    assert normalize_kind("html") == "html"


@pytest.mark.unit
def test_normalize_kind_invalid_returns_empty() -> None:
    assert normalize_kind("pdf") == ""
    assert normalize_kind("") == ""


@pytest.mark.unit
def test_is_safe_node_rejects_child_process() -> None:
    ok, reason = _is_safe_node("const cp = require('child_process'); cp.exec('ls');")
    assert not ok
    assert "child_process" in reason


@pytest.mark.unit
def test_is_safe_node_rejects_fs() -> None:
    ok, reason = _is_safe_node("const fs = require('fs'); fs.readFileSync('x');")
    assert not ok
    assert "fs" in reason


@pytest.mark.unit
def test_is_safe_node_accepts_pptxgenjs() -> None:
    ok, _ = _is_safe_node(
        "const PptxGenJS = require('pptxgenjs');\n"
        "const pres = new PptxGenJS();\n"
        "pres.addSlide().addText('hi');\n"
        "pres.writeFile({ fileName: 'output.pptx' });\n"
    )
    assert ok


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pptx_node_missing_marks_failure(tmp_path: Path) -> None:
    """node 不存在时返回 SkillError，不抛运行时异常。"""
    skill = SkillExecutor(
        Settings(
            storage_dir=tmp_path,
            skill_executor_build_dir=tmp_path / "skill_build",
            skill_executor_timeout_s=10,
            skill_executor_max_tokens=80_000,
            skill_node_bin="/non/existent/node-binary-xyz",
        )
    )
    code = (
        "const PptxGenJS = require('pptxgenjs');\n"
        "const pres = new PptxGenJS();\n"
        "pres.addSlide().addText('hi');\n"
        "pres.writeFile({ fileName: 'output.pptx' });\n"
    )
    llm = FakeLLM(code)
    with pytest.raises(SkillError):
        await skill.generate(llm=llm, artifact_type="pptx", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pptx_forbidden_token_raises(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    code = "const cp = require('child_process');\npres.writeFile({fileName:'x.pptx'});"
    llm = FakeLLM(code)
    with pytest.raises(SkillError, match=r"forbidden|execution failed"):
        await skill.generate(llm=llm, artifact_type="pptx", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="node / npm 未在 PATH",
)
async def test_pptx_generation_executes_pptxgenjs(tmp_path: Path) -> None:
    """mock LLM 返回最小可运行的 pptxgenjs 代码 → 真跑 node → 验证 .pptx 文件."""
    skill = SkillExecutor(_settings(tmp_path))
    code = (
        "const PptxGenJS = require('pptxgenjs');\n"
        "const pres = new PptxGenJS();\n"
        "pres.layout = 'LAYOUT_WIDE';\n"
        "const s1 = pres.addSlide();\n"
        "s1.addText('echo demo 1', { x: 0.5, y: 0.5, fontSize: 28 });\n"
        "const s2 = pres.addSlide();\n"
        "s2.addText('echo demo 2', { x: 0.5, y: 0.5, fontSize: 28 });\n"
        "const s3 = pres.addSlide();\n"
        "s3.addText('echo demo 3', { x: 0.5, y: 0.5, fontSize: 28 });\n"
        "pres.writeFile({ fileName: 'output.pptx' });\n"
    )
    llm = FakeLLM(code)
    art = await skill.generate(llm=llm, artifact_type="pptx", brief="生成 demo pptx")
    assert art.artifact_type == "pptx"
    assert art.file_path.endswith(".pptx")
    assert Path(art.file_path).stat().st_size > 8_000
    assert int(art.metadata.get("slide_count_hint", "0")) >= 3
