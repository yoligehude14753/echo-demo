"""SkillExecutor: 实现 SkillExecutorPort。

按 artifact_type 路由（**phase4-doc-skills 后**）：

| kind | 默认链路 | Legacy 回滚（USE_LEGACY_HTML_PPT=true） |
|---|---|---|
| pptx/ppt | LLM → 27 字段 JSON → ``node render.mjs`` + ib_master 模板 | LLM 直写 pptxgenjs js → node_executor |
| html     | LLM → Kami warm-parchment 整篇 HTML + 10 invariants 校验 | LLM → Tailwind dark theme HTML（直接落盘） |
| word     | python-docx — python_executor | — |
| xlsx     | openpyxl — python_executor | — |
| markdown | 直接写文件 — exec_text_to_file | — |
| txt      | 直接写文件 — exec_text_to_file | — |
| pdf      | fpdf2 + Noto Sans SC TTF — python_executor (env=ECHODESK_PDF_FONT_PATH) | — |

新版 HTML/PPT skill 移植自 echo experiments/2026-05-27_skill_path_compare/FINAL/，
设计哲学："LLM 只产数据 / 整页 HTML，布局美学由代码或 invariants 强约束"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

from app.adapters.llm.openai_compatible import LLMError
from app.adapters.skill.node_executor import exec_node_to_artifact
from app.adapters.skill.prompts import get_skill_prompt
from app.adapters.skill.python_executor import (
    ExecResult,
    exec_python_to_artifact,
    exec_text_to_file,
)
from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.artifact import SUPPORTED_KINDS, GeneratedArtifact, normalize_kind
from app.schemas.llm import ChatMessage
from app.schemas.skill_progress import SkillProgress

logger = logging.getLogger("echodesk.skill")

_CANONICAL_EXT: Final[dict[str, str]] = {
    "pptx": "pptx",
    "word": "docx",
    "xlsx": "xlsx",
    "html": "html",
    "markdown": "md",
    "pdf": "pdf",
    "txt": "txt",
}

# skill LLM 流式生成：连续 ``_STREAM_IDLE_TIMEOUT_S`` 秒没新 token 才判定为 stall。
# 不设 wall-clock 总时长——HTML one-pager / xlsx 长输出经常 8-15min，只要 LLM
# 还在吐 token 就一直接（用户原话 "只怕效率没打满，质量没打满"）。
_STREAM_IDLE_TIMEOUT_S: Final[float] = 90.0
# 建 SSE 连接的最长等待。给 1800s 是因为云雾偶发"连接建好但前 5min 没首 token"
# 的状况；首 token 到达后 idle window 接管,与此值无关。
_STREAM_CONNECT_TIMEOUT_S: Final[float] = 1800.0
# 每累积 N chars yield 一次 ``llm_chunk`` SkillProgress 事件。
# 太小（每个 token 一推）→ 前端 patchAssistantReply 太密导致 React 重渲染卡；
# 太大（>1k）→ 用户感知不到进度。200 chars ≈ 80-120 汉字，对应每 1-3s 一推。
_LLM_CHUNK_FLUSH_CHARS: Final[int] = 200

# 项目内置的中文字体（PDF 生成依赖）：backend/app/adapters/skill/fonts/...
_PDF_FONT_PATH: Final[Path] = Path(__file__).resolve().parent / "fonts" / "NotoSansSC-Regular.ttf"

# phase4-doc-skills：高质量 HTML / PPT skill 的二进制 & 脚本资产
# backend/app/adapters/skill/assets/ppt_ib_deck/{ib_master.pptx, render.mjs, package.json, ...}
_ASSETS_DIR: Final[Path] = Path(__file__).resolve().parent / "assets"
_PPT_IB_DECK_DIR: Final[Path] = _ASSETS_DIR / "ppt_ib_deck"
_PPT_IB_MASTER: Final[Path] = _PPT_IB_DECK_DIR / "ib_master.pptx"
_PPT_IB_RENDER_MJS: Final[Path] = _PPT_IB_DECK_DIR / "render.mjs"

# PPT IB deck 27 字段 schema（顺序与 schema.md / example_data.json 对齐）
_PPT_IB_DECK_FIELDS: Final[tuple[str, ...]] = (
    "cover_title",
    "cover_subtitle",
    "disclaimer_body",
    "es_b1",
    "es_b2",
    "es_b3",
    "kpi1_value",
    "kpi2_value",
    "kpi3_value",
    "kpi4_value",
    "th_lead",
    "th_b1",
    "th_b2",
    "th_b3",
    "mk_lead",
    "mk_b1",
    "mk_b2",
    "cp_r1",
    "cp_r2",
    "cp_r3",
    "rk_b1",
    "rk_b2",
    "rk_b3",
    "rec_action",
    "rec_target",
    "rec_upside",
    "closing_tagline",
)

# 日文片假名（U+30A0..U+30FF）—— M2.7 偶尔会蹦日语，必须拒绝（INTEGRATE_PROMPT 6.6）
_KATAKANA_RE: Final[re.Pattern[str]] = re.compile(r"[\u30A0-\u30FF]")
# Emoji 检测（覆盖主要 emoji 区段 + 杂项符号）—— HTML one-pager 不许 emoji
_EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    "["
    "\U0001f300-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "]"
)


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


# ──────────────────────────────────────────────────────────────────────────
# phase4-doc-skills：HTML one-pager 抽取 + invariants 校验
# ──────────────────────────────────────────────────────────────────────────


def _extract_html_document(text: str) -> str:
    """从 LLM 输出里抽出 ``<!doctype html>...</html>`` 整段（容忍前后说明文字）。

    匹配规则：
    - 大小写不敏感找到第一个 ``<!doctype`` 或 ``<html``
    - 找到最后一个 ``</html>``（含 ``>``）
    - 范围内做为整段返回；找不到则返回原文（让下游 invariant 校验抛错）
    """
    low = text.lower()
    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    end = low.rfind("</html>")
    if start == -1 or end == -1 or end <= start:
        return text.strip()
    return text[start : end + len("</html>")].strip()


def _check_html_one_pager_invariants(html: str) -> list[str]:
    """对 Kami warm-parchment one-pager 做 10 invariants 子集校验，返回违规列表。

    返回值为空 → 通过；非空 → 上层抛 SkillError 让 LLM 重试或回退。

    校验项（参考 echo FINAL/html_one_pager/SKILL.md §"内容质量门禁"）：
    1. 必须以 ``<!doctype`` 或 ``<html`` 开头（已被 _extract_html_document 收敛）
    2. 总字符数 ≥ 6000（含 markup；SKILL.md 目标 12000，留宽余量给 demo / 中小 brief）
    3. 至少 3 个 ``<svg`` block
    4. 不许 ``rgba(``（WeasyPrint 双 rect bug；invariant #8）
    5. 必须含 ``#f5f4ed`` 或 ``var(--parchment)``（背景锚点；invariant #1）
    6. 不许日文片假名（M2.7 偶发；不在 Kami 10 invariants 但在 INTEGRATE_PROMPT 6.6）
    7. 不许 emoji（编辑设计语言禁用 emoji；SKILL.md 内容质量门禁）

    *未校验*：line-height、letter-spacing、字体族细节 —— 这些靠 prompt 约束 + 人审，
    自动校验误报率太高。
    """
    violations: list[str] = []
    low = html.lower()

    if not (low.startswith("<!doctype") or "<html" in low[:500]):
        violations.append("缺少 <!doctype> 或 <html> 起始标记")
    if len(html) < 6000:
        violations.append(f"内容太短（{len(html)} chars，要求 ≥ 6000）")
    svg_count = low.count("<svg")
    if svg_count < 3:
        violations.append(f"inline SVG 太少（{svg_count}，要求 ≥ 3）")
    if "rgba(" in low:
        violations.append("含 rgba(...)（违反 Kami invariant #8）")
    if "#f5f4ed" not in low and "var(--parchment)" not in low:
        violations.append("缺少 #f5f4ed / var(--parchment) 背景锚点")
    if _KATAKANA_RE.search(html):
        violations.append("含日文片假名（M2.7 LLM 偶发）")
    if _EMOJI_RE.search(html):
        violations.append("含 emoji（编辑设计语言禁用）")
    return violations


# ──────────────────────────────────────────────────────────────────────────
# phase4-doc-skills：IB deck JSON 解析（容忍前后说明 + 多个 fenced block）
# ──────────────────────────────────────────────────────────────────────────


_JSON_OBJ_RE: Final[re.Pattern[str]] = re.compile(r"\{[\s\S]*\}")


def _parse_ib_deck_json(raw: str) -> dict[str, str]:
    """从 LLM 输出里抽出 JSON 对象。

    优先级：
    1. ``json.loads(raw)`` 直接成功
    2. 剥 markdown 围栏后再 ``json.loads``
    3. 用 ``{...}`` greedy 正则找最大 JSON 块再 loads

    全部失败 → ``SkillError``。
    """
    candidates: list[str] = [raw.strip()]
    stripped = _strip_code_fence(raw, text_mode=False)
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    # 再加一个 text_mode=True 的剥围栏结果（避免 _CODE_START_PREFIXES 启发吞掉首字段）
    stripped_text = _strip_code_fence(raw, text_mode=True)
    if stripped_text and stripped_text not in candidates:
        candidates.append(stripped_text)
    m = _JSON_OBJ_RE.search(raw)
    if m:
        candidates.append(m.group(0))

    last_err: Exception | None = None
    for c in candidates:
        try:
            parsed = json.loads(c)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(parsed, dict):
            # 把所有 value 归一为 string；保护 None / 数字 等异常输入
            return {str(k): ("" if v is None else str(v)) for k, v in parsed.items()}
        last_err = TypeError(f"JSON 顶层不是 object: {type(parsed).__name__}")

    raise SkillError(f"无法解析 ib_pptx JSON: {last_err}")


class SkillExecutor:
    """实现 ports.skill.SkillExecutorPort（7 产物生成 + 别名归一）。

    phase4-doc-skills（2026-05-28）后，HTML / PPT 默认走"echo FINAL 高质量 skill"路径：
    - HTML → ``_generate_html_one_pager``：LLM 直出 Kami warm-parchment 整页 + 10 invariants
    - PPT  → ``_generate_ib_pptx``：LLM 出 27 字段 JSON → ``node render.mjs`` 渲染

    ``Settings.use_legacy_html_pptx=True`` 时回滚到旧版（LLM 直写代码 → executor）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._build_root = Path(settings.skill_executor_build_dir).expanduser()
        self._node_modules_root = self._build_root.parent / "skill_node_deps"
        self._timeout_s = float(settings.skill_executor_timeout_s)
        self._max_tokens = settings.skill_executor_max_tokens
        self._node_bin = settings.skill_node_bin
        self._npm_bin = "npm"
        self._use_legacy_html_pptx = bool(getattr(settings, "use_legacy_html_pptx", False))

    async def generate(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact:
        """非流式入口（向后兼容）：消费 ``generate_stream`` 拿到 ``done`` 事件返回 artifact。

        异常路径：``generate_stream`` 内部捕获 ``SkillError`` / ``LLMError`` 后会
        yield 一条 ``stage="error"`` 进度事件再 re-raise，原始异常类型透传上来，
        让 ``api/artifacts.py`` 仍能按 ``SkillError`` (400) / ``LLMError`` (502)
        分流。
        """
        final: GeneratedArtifact | None = None
        async for ev in self.generate_stream(
            llm=llm,
            artifact_type=artifact_type,
            brief=brief,
            extra_instructions=extra_instructions,
        ):
            if ev.stage == "done" and ev.artifact is not None:
                final = ev.artifact
        if final is None:
            raise SkillError("generate_stream 完成但未产出 artifact（缺少 done 事件）")
        return final

    async def generate_stream(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> AsyncIterator[SkillProgress]:
        """流式入口：yield 一系列 ``SkillProgress`` 事件，让前端能看到过程性内容。

        阶段序列（happy path，HTML one-pager 为例）：

        - ``prompt_build`` → 进入路由 / 准备 system prompt
        - ``llm_stream_start`` → 即将调 LLM
        - ``llm_chunk`` × N → LLM 每累积 200 chars 推一次（前端取尾部展示）
        - ``llm_stream_done`` → LLM 全文已收到（``text`` 含完整内容）
        - ``invariants_check`` → 校验 HTML / JSON
        - ``executor_run`` → 调 node / python / 直接落盘
        - ``saved`` → 产物已落盘
        - ``done`` → 携带 ``GeneratedArtifact``

        异常路径（``SkillError`` / ``LLMError``）：catch → yield ``stage="error"``
        → re-raise（保留原始异常类型供上层 API 分流）。

        Fallback：HTML one-pager 路径 ``SkillError`` 时（如 invariants 违反）自动
        降级 legacy ``_generate_via_default_pipeline_stream``；fallback 事件流
        与正常 happy path 同型，只是 ``done.artifact.metadata`` 会多
        ``legacy_pipeline="true"`` + ``fallback_reason=...``。
        """
        if artifact_type.lower().strip() not in SUPPORTED_KINDS:
            raise SkillError(
                f"unsupported artifact_type: {artifact_type} (supported: {sorted(SUPPORTED_KINDS)})"
            )
        kind = normalize_kind(artifact_type)
        if not kind:
            raise SkillError(f"cannot normalize artifact_type: {artifact_type}")

        artifact_id = f"{kind}-{uuid.uuid4().hex[:10]}"
        build_dir = self._build_root / artifact_id
        build_dir.mkdir(parents=True, exist_ok=True)

        yield SkillProgress(
            stage="prompt_build",
            msg=f"准备 {kind} prompt 中…",
        )

        try:
            if kind == "html" and not self._use_legacy_html_pptx:
                try:
                    async for ev in self._generate_html_one_pager_stream(
                        llm=llm,
                        brief=brief,
                        extra_instructions=extra_instructions,
                        artifact_id=artifact_id,
                        build_dir=build_dir,
                    ):
                        yield ev
                    return
                except SkillError as e:
                    # 高质量 HTML invariants 违反 / 抽取失败 → 降级 legacy。
                    # LLMError 不在此处 catch（LLM 不可达时 legacy 也会失败,直接外抛）。
                    logger.warning(
                        "html one-pager 失败，降级 legacy: %s (artifact_id=%s)",
                        e,
                        artifact_id,
                    )
                    yield SkillProgress(
                        stage="prompt_build",
                        msg=f"高质量 HTML 失败({e})，降级 legacy 流水线…",
                    )
                    async for ev in self._generate_via_default_pipeline_stream(
                        llm=llm,
                        kind=kind,
                        brief=brief,
                        extra_instructions=extra_instructions,
                        artifact_id=artifact_id,
                        build_dir=build_dir,
                        legacy_fallback_reason=str(e),
                    ):
                        yield ev
                    return
            if kind == "pptx" and not self._use_legacy_html_pptx:
                # PPT 不加 fallback：ib_deck JSON 字段校验失败常意味着 LLM 严重跑偏。
                async for ev in self._generate_ib_pptx_stream(
                    llm=llm,
                    brief=brief,
                    extra_instructions=extra_instructions,
                    artifact_id=artifact_id,
                    build_dir=build_dir,
                ):
                    yield ev
                return

            async for ev in self._generate_via_default_pipeline_stream(
                llm=llm,
                kind=kind,
                brief=brief,
                extra_instructions=extra_instructions,
                artifact_id=artifact_id,
                build_dir=build_dir,
            ):
                yield ev
        except (SkillError, LLMError) as e:
            yield SkillProgress(stage="error", error=str(e))
            raise

    # ─────────────────────────────────────────────────────────────────────
    # 默认 / legacy 流水线：LLM → 剥围栏 → exec_for_kind → 产物
    # 用于 word / xlsx / markdown / txt / pdf；也作为 HTML/PPT 的 legacy 回滚。
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_via_default_pipeline_stream(
        self,
        *,
        llm: LLMPort,
        kind: str,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
        legacy_fallback_reason: str | None = None,
    ) -> AsyncIterator[SkillProgress]:
        """Stream 版默认流水线。yield ``llm_stream_*`` + ``executor_run`` + ``saved`` + ``done``。

        ``legacy_fallback_reason`` 非空时表示这是 HTML one-pager 失败后的降级路径，
        会在 metadata 写 ``legacy_pipeline="true"`` + ``fallback_reason``。
        """
        sys_prompt = get_skill_prompt(kind, legacy=self._use_legacy_html_pptx)
        yield SkillProgress(
            stage="llm_stream_start",
            msg=f"调用 {self._settings.llm_main_model}（{kind} 默认流水线）…",
        )

        llm_content = ""
        llm_latency_ms = 0.0
        async for ev in self._call_llm_stream(llm, sys_prompt, brief, extra_instructions):
            yield ev
            if ev.stage == "llm_stream_done":
                llm_content = ev.text or ""
                llm_latency_ms = ev.latency_ms or 0.0

        code = _strip_code_fence(llm_content, text_mode=kind in _TEXT_KINDS)
        ext = _CANONICAL_EXT[kind]

        yield SkillProgress(
            stage="executor_run",
            tool=_executor_tool_for_kind(kind),
            msg=f"执行 {kind} 生成器（{_executor_tool_for_kind(kind)}）…",
        )
        result = await self._exec_for_kind(kind, code, build_dir, ext)
        if not result.success or result.output_path is None:
            raise SkillError(f"skill {kind} execution failed: {result.stderr[:400]}")
        output_path = result.output_path

        title = _make_title(brief)
        metadata: dict[str, str] = {
            "kind": kind,
            "model": self._settings.llm_main_model,
            "exec_elapsed_s": f"{result.elapsed_s:.2f}",
            "code_size": str(len(code)),
        }
        if legacy_fallback_reason is not None:
            metadata["legacy_pipeline"] = "true"
            metadata["fallback_reason"] = legacy_fallback_reason[:200]
        elif self._use_legacy_html_pptx and kind in {"html", "pptx"}:
            metadata["legacy_pipeline"] = "true"

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

        artifact = GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=kind,
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for(ext),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_latency_ms + result.elapsed_s * 1000.0,
            model=self._settings.llm_main_model,
            metadata=metadata,
        )
        yield SkillProgress(stage="saved", msg=f"产物 {artifact_id} 已保存")
        yield SkillProgress(stage="done", artifact=artifact)

    # ─────────────────────────────────────────────────────────────────────
    # phase4-doc-skills：HTML one-pager（Kami warm-parchment）
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_html_one_pager_stream(
        self,
        *,
        llm: LLMPort,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
    ) -> AsyncIterator[SkillProgress]:
        """Stream 版 Kami one-pager 生成。yield ``llm_stream_*`` + ``invariants_check``
        + ``executor_run`` + ``saved`` + ``done``。

        失败路径：
        - 含日文片假名 → ``SkillError("LLM 输出含日文片假名")``
        - invariants 不满足（rgba / emoji / 没 #f5f4ed / SVG < 3 等）→ ``SkillError(...)``

        SkillError 由上层 ``generate_stream`` 捕获后走 legacy fallback。
        """
        sys_prompt = get_skill_prompt("html", legacy=False)
        yield SkillProgress(
            stage="llm_stream_start",
            msg=f"调用 {self._settings.llm_main_model}（Kami one-pager）…",
        )

        llm_content = ""
        llm_latency_ms = 0.0
        async for ev in self._call_llm_stream(llm, sys_prompt, brief, extra_instructions):
            yield ev
            if ev.stage == "llm_stream_done":
                llm_content = ev.text or ""
                llm_latency_ms = ev.latency_ms or 0.0

        yield SkillProgress(stage="invariants_check", msg="校验 HTML 10 invariants 中…")
        html = _strip_code_fence(llm_content, text_mode=True)
        html = _extract_html_document(html)

        violations = _check_html_one_pager_invariants(html)
        if violations:
            raise SkillError(f"HTML one-pager invariant 违反: {'; '.join(violations[:3])}")

        yield SkillProgress(
            stage="executor_run",
            tool="exec_text_to_file",
            msg="写入 HTML 文件…",
        )
        output_path = build_dir / "output.html"
        output_path.write_text(html, encoding="utf-8")

        title = _make_title(brief)
        metadata: dict[str, str] = {
            "kind": "html",
            "model": self._settings.llm_main_model,
            "exec_elapsed_s": "0.00",
            "code_size": str(len(html)),
            "skill_variant": "kami_one_pager",
            "chars": str(len(html)),
            "svg_count": str(html.lower().count("<svg")),
            "has_parchment": "true",
        }
        self._write_meta(build_dir, title=title, kind="html", ext="html")

        artifact = GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="html",
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for("html"),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_latency_ms,
            model=self._settings.llm_main_model,
            metadata=metadata,
        )
        yield SkillProgress(stage="saved", msg=f"产物 {artifact_id} 已保存")
        yield SkillProgress(stage="done", artifact=artifact)

    # ─────────────────────────────────────────────────────────────────────
    # phase4-doc-skills：14 页投行风 PPT（ib_master + docxtemplater）
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_ib_pptx_stream(
        self,
        *,
        llm: LLMPort,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
    ) -> AsyncIterator[SkillProgress]:
        """Stream 版 14 页 ib_pptx 生成。yield ``llm_stream_*`` + ``invariants_check``
        + ``executor_run`` (node render.mjs) + ``saved`` + ``done``。
        """
        if not _PPT_IB_MASTER.exists() or not _PPT_IB_RENDER_MJS.exists():
            raise SkillError(
                "ppt_ib_deck assets 缺失，期望在 "
                f"{_PPT_IB_DECK_DIR}（ib_master.pptx + render.mjs）。"
                "请在 backend/app/adapters/skill/assets/ppt_ib_deck/ 跑 npm install。"
            )

        sys_prompt = get_skill_prompt("pptx", legacy=False)
        yield SkillProgress(
            stage="llm_stream_start",
            msg=f"调用 {self._settings.llm_main_model}（ib_pptx 27 字段 JSON）…",
        )

        raw = ""
        llm_latency_ms = 0.0
        async for ev in self._call_llm_stream(llm, sys_prompt, brief, extra_instructions):
            yield ev
            if ev.stage == "llm_stream_done":
                raw = ev.text or ""
                llm_latency_ms = ev.latency_ms or 0.0

        yield SkillProgress(
            stage="invariants_check",
            msg="校验 27 字段 JSON / 片假名 / 字段完整性…",
        )
        if _KATAKANA_RE.search(raw):
            raise SkillError(
                "LLM 输出含日文片假名（M2.7 偶发），拒绝渲染；建议重试 "
                "（或 USE_LEGACY_HTML_PPT=true 回滚）"
            )

        data = _parse_ib_deck_json(raw)
        missing = [f for f in _PPT_IB_DECK_FIELDS if not data.get(f)]
        if missing:
            raise SkillError(f"ppt_ib_deck JSON 缺失字段: {missing[:5]} (共 {len(missing)} 个)")

        data_path = build_dir / "data.json"
        data_path.write_text(
            json.dumps({k: data[k] for k in _PPT_IB_DECK_FIELDS}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        yield SkillProgress(
            stage="executor_run",
            tool="node_render_mjs",
            msg="渲染 14 页投行风 PPT（node render.mjs ib_master.pptx）…",
        )
        output_path = build_dir / "output.pptx"
        render_result = await self._run_ib_render(
            data_path=data_path,
            output_path=output_path,
            build_dir=build_dir,
        )
        if not render_result.success:
            raise SkillError(f"ib_pptx render 失败: {render_result.stderr[:400]}")

        title = _make_title(brief)
        metadata: dict[str, str] = {
            "kind": "pptx",
            "model": self._settings.llm_main_model,
            "exec_elapsed_s": f"{render_result.elapsed_s:.2f}",
            "skill_variant": "ib_deck_v3",
            "field_count": str(len(_PPT_IB_DECK_FIELDS)),
            "slide_count_hint": "14",
            "code_size": str(len(raw)),
        }
        self._write_meta(build_dir, title=title, kind="pptx", ext="pptx")

        artifact = GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="pptx",
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for("pptx"),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_latency_ms + render_result.elapsed_s * 1000.0,
            model=self._settings.llm_main_model,
            metadata=metadata,
        )
        yield SkillProgress(stage="saved", msg=f"产物 {artifact_id} 已保存")
        yield SkillProgress(stage="done", artifact=artifact)

    async def _run_ib_render(
        self,
        *,
        data_path: Path,
        output_path: Path,
        build_dir: Path,
    ) -> ExecResult:
        """串行调用 ``node render.mjs``（避免 npm 锁冲突；INTEGRATE_PROMPT 6.6）。

        前置检查 node 二进制 + 母版 + node_modules；缺一返回 ExecResult.success=False。
        """
        import asyncio
        import os

        if not shutil.which(self._node_bin) and not Path(self._node_bin).is_file():
            return ExecResult(
                success=False,
                output_path=None,
                stderr=f"node binary not executable: {self._node_bin}",
                elapsed_s=0.0,
            )
        deck_node_modules = _PPT_IB_DECK_DIR / "node_modules"
        if not deck_node_modules.exists():
            return ExecResult(
                success=False,
                output_path=None,
                stderr=(
                    "ppt_ib_deck node_modules 不存在；请在 "
                    f"{_PPT_IB_DECK_DIR} 跑 `npm install`（或跑 scripts/install-backend.sh）"
                ),
                elapsed_s=0.0,
            )

        t0 = time.monotonic()

        def _run() -> tuple[int, str]:
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(Path.home()),
                "NODE_PATH": str(deck_node_modules.resolve()),
            }
            proc = subprocess.run(
                [
                    self._node_bin,
                    str(_PPT_IB_RENDER_MJS),
                    str(_PPT_IB_MASTER),
                    str(data_path),
                    str(output_path),
                ],
                cwd=str(build_dir),
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=env,
                check=False,
            )
            return proc.returncode, proc.stderr or proc.stdout

        try:
            rc, stderr = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                success=False,
                output_path=None,
                stderr=f"timeout after {self._timeout_s}s: {e}",
                elapsed_s=self._timeout_s,
            )
        except FileNotFoundError as e:
            return ExecResult(
                success=False,
                output_path=None,
                stderr=f"node not on PATH: {e}",
                elapsed_s=0.0,
            )

        elapsed = time.monotonic() - t0
        if rc == 0 and output_path.exists() and output_path.stat().st_size > 8_000:
            return ExecResult(success=True, output_path=output_path, stderr="", elapsed_s=elapsed)
        return ExecResult(
            success=False,
            output_path=None,
            stderr=f"rc={rc} stderr={stderr[:600]}",
            elapsed_s=elapsed,
        )

    async def _call_llm_stream(
        self,
        llm: LLMPort,
        sys_prompt: str,
        brief: str,
        extra_instructions: str | None,
    ) -> AsyncIterator[SkillProgress]:
        """流式调 LLM 并按 ~200 chars 节流推进度事件。

        - 保留 ``_STREAM_IDLE_TIMEOUT_S`` 空闲检测（连续 90s 没新 chunk 才判 stall）
        - 每累积 ``_LLM_CHUNK_FLUSH_CHARS`` 字符 yield 一次 ``stage="llm_chunk"``，
          ``text`` 是「目前累积的全部内容」（前端只取尾部展示，避免拼接错位）
        - 末尾 yield ``stage="llm_stream_done"``：``text`` 完整内容、``total_chars``、
          ``latency_ms``。调用方据 done 事件取最终 content 进入后续阶段。

        失败：``TimeoutError`` 转换为 ``LLMError``，由上层 ``generate_stream`` catch
        → yield error → re-raise（保留 LLMError 类型给 api/artifacts.py 分流到 502）。

        ── 为什么用流式 ─────────────────────────────────────────────────
        skill 让 LLM 一次性产出 HTML one-pager（6000+ 字符 + 3 SVG）或
        PPT 27 字段 JSON，yunwu/MiniMax-M2.7 上偶发 4-5 分钟才出完。
        非流式 ``llm.chat(timeout_s=300)`` 把整个请求包在一个 wait_for
        里 → 上游慢吐 token 也会被一刀切，前端拿到 ``timeout after 300s``。

        改成 ``chat_stream`` + idle-timeout：只要 ``_STREAM_IDLE_TIMEOUT_S``
        秒内还有新 chunk 进来就一直等，整体 wall-clock 上限 = ``_timeout_s``。
        """
        user_msg = brief
        if extra_instructions:
            user_msg += "\n\n额外指令：\n" + extra_instructions
        messages = [
            ChatMessage(role="system", content=sys_prompt),
            ChatMessage(role="user", content=user_msg),
        ]
        t_start = time.monotonic()
        chunks: list[str] = []
        last_flush_total = 0
        # chat_stream 内部仅用 timeout_s 限制 *建立 SSE 连接* 的最长等待；连接建好后
        # 每个 chunk 由我们这里的 idle window 把守。给 chat_stream 一个**远大于
        # ``self._timeout_s``** 的 connect timeout，避免长输出在快收完时被一刀切。
        agen = llm.chat_stream(
            messages,
            max_tokens=self._max_tokens,
            temperature=0.4,
            timeout_s=_STREAM_CONNECT_TIMEOUT_S,
        )
        # ── 为什么没有 wall-clock 兜底 ────────────────────────────────────
        # E2E 实测（2026-05-28 17:58）：MiniMax-M2.7 输出 ~25k chars 的 HTML
        # one-pager 单次 wall-clock 已逼近 300s。HTML invariants 要求 ≥6000
        # chars + ≥3 SVG，xlsx Python 代码块也常超 15k chars。用户原话：
        # "只怕效率没打满，质量没打满"——意思是只要 LLM 还在吐 token 就让它
        # 吐完，不要为了"看起来不卡"提前砍掉。idle window 已经足够区分
        # "LLM 还在生成" vs "上游真挂了/被防火墙阻断"。
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(agen.__anext__(), timeout=_STREAM_IDLE_TIMEOUT_S)
                except StopAsyncIteration:
                    break
                except TimeoutError as e:
                    elapsed = time.monotonic() - t_start
                    raise LLMError(
                        f"{self._settings.llm_main_model} skill stream idle timeout"
                        f" after {_STREAM_IDLE_TIMEOUT_S:.0f}s without new tokens"
                        f" (received {sum(len(c) for c in chunks)} chars in"
                        f" {elapsed:.0f}s before stall)"
                    ) from e
                chunks.append(chunk)
                total = sum(len(c) for c in chunks)
                if total - last_flush_total >= _LLM_CHUNK_FLUSH_CHARS:
                    acc = "".join(chunks)
                    yield SkillProgress(
                        stage="llm_chunk",
                        text=acc,
                        total_chars=len(acc),
                    )
                    last_flush_total = total
        finally:
            close = getattr(agen, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        content = "".join(chunks)
        yield SkillProgress(
            stage="llm_stream_done",
            text=content,
            total_chars=len(content),
            latency_ms=(time.monotonic() - t_start) * 1000.0,
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


def _executor_tool_for_kind(kind: str) -> str:
    """返回 default pipeline 对应的执行器名（用于 ``SkillProgress.tool`` 字段）。

    保持与 ``SkillExecutor._exec_for_kind`` 的路由一致：html/markdown/txt 走
    ``exec_text_to_file``，pptx 走 ``exec_node_to_artifact``，其它走
    ``exec_python_to_artifact``。
    """
    if kind in {"html", "markdown", "txt"}:
        return "exec_text_to_file"
    if kind == "pptx":
        return "exec_node_to_artifact"
    return "exec_python_to_artifact"


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
