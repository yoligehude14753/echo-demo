"""SkillExecutor: 实现 SkillExecutorPort。

按 artifact_type 路由：
- pptx/ppt → pptxgenjs (Node) - node_executor
- word     → python-docx  - python_executor
- xlsx/excel → openpyxl    - python_executor
- html     → 直接写文件    - python_executor.exec_text_to_file
- markdown → 直接写文件    - python_executor.exec_text_to_file
- txt      → 直接写文件    - python_executor.exec_text_to_file
- pdf      → fpdf2 + Noto Sans SC TTF - python_executor (env=ECHODESK_PDF_FONT_PATH)
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Final

from app.adapters.skill.node_executor import exec_node_to_artifact
from app.adapters.skill.prompts import SKILL_PROMPTS
from app.adapters.skill.python_executor import (
    ExecResult,
    exec_python_to_artifact,
    exec_text_to_file,
)
from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.artifact import SUPPORTED_KINDS, GeneratedArtifact, normalize_kind
from app.schemas.llm import ChatMessage

_CANONICAL_EXT: Final[dict[str, str]] = {
    "pptx": "pptx",
    "word": "docx",
    "xlsx": "xlsx",
    "html": "html",
    "markdown": "md",
    "pdf": "pdf",
    "txt": "txt",
}

# 项目内置的中文字体（PDF 生成依赖）：backend/app/adapters/skill/fonts/...
_PDF_FONT_PATH: Final[Path] = Path(__file__).resolve().parent / "fonts" / "NotoSansSC-Regular.ttf"


class SkillError(RuntimeError):
    pass


_CODE_START_PREFIXES = (
    "from ",
    "import ",
    "#!/usr/bin",
    "<!doctype",
    "<!DOCTYPE",
    "<html",
    "const ",
    "let ",
    "var ",
    "require(",
    "function ",
    "(async",
    "async function",
)

# markdown / txt 是 LLM 直出文本而非可执行代码；剥围栏即可，不应该
# 用「跳到第一行代码 token」的启发式（会吞掉文档首行普通文本）。
_TEXT_KINDS: Final[frozenset[str]] = frozenset({"markdown", "txt", "html"})


def _strip_code_fence(text: str, *, text_mode: bool = False) -> str:
    """剥掉 ```python / ```html / ```javascript / ```js 围栏；
    并在 LLM 加前导自然语言时（M2.7 thinking 残留）自动跳到第一行代码。

    ``text_mode=True`` 用于 markdown / txt / html 这类「LLM 直出文档」场景：
    只剥围栏，不做 _CODE_START_PREFIXES 启发跳转（否则会吞掉文档首段）。
    """
    text = text.strip()
    # 1. 围栏剥离
    if "```" in text:
        first = text.find("```")
        nl = text.find("\n", first)
        if nl != -1:
            close = text.find("```", nl + 1)
            if close != -1:
                return text[nl + 1 : close].strip()
            return text[nl + 1 :].strip()
    if text_mode:
        return text
    # 2. 没有围栏：找第一个代码起始 token
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if any(stripped.startswith(p) for p in _CODE_START_PREFIXES):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def _make_title(brief: str, max_len: int = 40) -> str:
    """从 brief 提取标题：去前后空白、合并多空白、按 char 数截前 ``max_len`` 字（CJK 友好）。"""
    cleaned = " ".join(brief.split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "…"


class SkillExecutor:
    """实现 ports.skill.SkillExecutorPort（7 产物生成 + 别名归一）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._build_root = Path(settings.skill_executor_build_dir).expanduser()
        self._node_modules_root = self._build_root.parent / "skill_node_deps"
        self._timeout_s = float(settings.skill_executor_timeout_s)
        self._max_tokens = settings.skill_executor_max_tokens
        self._node_bin = settings.skill_node_bin
        self._npm_bin = "npm"

    async def generate(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact:
        if artifact_type.lower().strip() not in SUPPORTED_KINDS:
            raise SkillError(
                f"unsupported artifact_type: {artifact_type} (supported: {sorted(SUPPORTED_KINDS)})"
            )
        kind = normalize_kind(artifact_type)
        if not kind:
            raise SkillError(f"cannot normalize artifact_type: {artifact_type}")

        sys_prompt = SKILL_PROMPTS[kind]
        user_msg = brief
        if extra_instructions:
            user_msg += "\n\n额外指令：\n" + extra_instructions

        t_start = time.monotonic()
        resp = await llm.chat(
            [
                ChatMessage(role="system", content=sys_prompt),
                ChatMessage(role="user", content=user_msg),
            ],
            max_tokens=self._max_tokens,
            temperature=0.4,
            timeout_s=self._timeout_s,
        )
        code = _strip_code_fence(resp.content, text_mode=kind in _TEXT_KINDS)
        gen_latency_ms = (time.monotonic() - t_start) * 1000.0

        artifact_id = f"{kind}-{uuid.uuid4().hex[:10]}"
        build_dir = self._build_root / artifact_id
        build_dir.mkdir(parents=True, exist_ok=True)

        ext = _CANONICAL_EXT[kind]
        result = await self._exec_for_kind(kind, code, build_dir, ext)
        if not result.success or result.output_path is None:
            raise SkillError(f"skill {kind} execution failed: {result.stderr[:400]}")
        output_path = result.output_path
        exec_elapsed = result.elapsed_s

        title = _make_title(brief)

        metadata: dict[str, str] = {
            "kind": kind,
            "model": resp.model,
            "exec_elapsed_s": f"{exec_elapsed:.2f}",
            "code_size": str(len(code)),
        }

        bag = code.lower()
        if kind == "html":
            metadata["chars"] = str(len(code))
            metadata["has_tailwind"] = str("tailwindcss" in bag)
            metadata["has_svg"] = str("<svg" in bag)
        elif kind == "xlsx":
            metadata["formula_count"] = str(len(re.findall(r"=[A-Z]+\(", code)))
        elif kind == "pptx":
            metadata["slide_count_hint"] = str(len(re.findall(r"\.addSlide\(", code)))
            metadata["table_count_hint"] = str(len(re.findall(r"\.addTable\(", code)))
        elif kind == "markdown":
            metadata["chars"] = str(len(code))
            metadata["heading_count"] = str(len(re.findall(r"(?m)^#{1,6}\s", code)))
            metadata["table_count"] = str(len(re.findall(r"(?m)^\s*\|.+\|\s*$", code)))
        elif kind == "txt":
            metadata["chars"] = str(len(code))
            metadata["line_count"] = str(code.count("\n") + 1)
        elif kind == "pdf":
            metadata["pages_hint"] = str(len(re.findall(r"\.add_page\(\)", code)))
            metadata["uses_noto_font"] = str("noto" in bag)

        self._write_meta(build_dir, title=title, kind=kind, ext=ext)

        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=kind,
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for(ext),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=gen_latency_ms + exec_elapsed * 1000.0,
            model=resp.model,
            metadata=metadata,
        )

    async def _exec_for_kind(self, kind: str, code: str, build_dir: Path, ext: str) -> ExecResult:
        """按 canonical kind 路由到对应执行器。

        Node 路径返回 ``NodeExecResult``，结构与 ``ExecResult`` 同形
        （success / output_path / stderr / elapsed_s），转一层 ``ExecResult`` 满足类型契约。
        """
        if kind in {"html", "markdown", "txt"}:
            return await exec_text_to_file(code, build_dir, ext)
        if kind == "pptx":
            node_result = await exec_node_to_artifact(
                code,
                build_dir,
                node_modules_root=self._node_modules_root,
                expected_ext=ext,
                node_bin=self._node_bin,
                npm_bin=self._npm_bin,
                timeout_s=self._timeout_s,
            )
            return ExecResult(
                success=node_result.success,
                output_path=node_result.output_path,
                stderr=node_result.stderr,
                elapsed_s=node_result.elapsed_s,
            )
        if kind == "pdf":
            env = {"ECHODESK_PDF_FONT_PATH": str(_PDF_FONT_PATH.resolve())}
            return await exec_python_to_artifact(
                code,
                build_dir,
                expected_ext=ext,
                timeout_s=self._timeout_s,
                env=env,
            )
        # word / xlsx
        return await exec_python_to_artifact(
            code, build_dir, expected_ext=ext, timeout_s=self._timeout_s
        )

    @staticmethod
    def _write_meta(build_dir: Path, *, title: str, kind: str, ext: str) -> None:
        """持久化 artifact 元信息（title / kind / ext），供 download endpoint 拼 filename。"""
        meta = {"title": title, "artifact_type": kind, "ext": ext}
        with contextlib.suppress(OSError):  # 写失败时降级（download 回退到 output.ext）
            (build_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )


def _mime_for(ext: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "html": "text/html",
        "md": "text/markdown",
        "txt": "text/plain",
        "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")
