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

import contextlib
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

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


@dataclass
class _LLMOutput:
    """LLM 一次 chat 调用的可观察结果（用于产物 metadata + 日志）。"""

    content: str  # 已剥围栏的输出
    model: str
    latency_ms: float


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

        # 高质量路径：默认 HTML 走 Kami one-pager；默认 PPT 走 ib_master JSON 渲染。
        # use_legacy_html_pptx=True 时回滚到 _generate_via_default_pipeline。
        #
        # 用户 2026-05-28 反馈：「生成 HTML 一直报 400」。根因：one-pager 有 7 条
        # invariants（chars≥6000 / SVG≥3 / 必须含 #f5f4ed / 不许 rgba / 不许 emoji
        # 等），任何一条违反 SkillError 直接 400，**没有 fallback**。M2.7 LLM 输
        # 出常见 4000-5000 chars + 2 个 SVG → 命中率高。
        #
        # 修法：one-pager / ib_pptx 失败时 catch SkillError 自动降级到 legacy
        # `_generate_via_default_pipeline`（LLM 写 raw HTML / pptxgenjs），保证
        # 用户拿到东西。legacy 失败才真往上抛。
        if kind == "html" and not self._use_legacy_html_pptx:
            try:
                return await self._generate_html_one_pager(
                    llm=llm,
                    brief=brief,
                    extra_instructions=extra_instructions,
                    artifact_id=artifact_id,
                    build_dir=build_dir,
                )
            except SkillError as e:
                logger.warning(
                    "html one-pager 失败，降级 legacy: %s (artifact_id=%s)",
                    e,
                    artifact_id,
                )
                # 复用同一 build_dir：上一轮 LLM 写的 output.html / meta.json 会被
                # 下一轮覆盖；不另开 build_dir 避免产物 id 错位
                art = await self._generate_via_default_pipeline(
                    llm=llm,
                    kind=kind,
                    brief=brief,
                    extra_instructions=extra_instructions,
                    artifact_id=artifact_id,
                    build_dir=build_dir,
                )
                # 标记为 fallback 产物，前端 / 日志可据此提示用户"用了降级路径"
                art.metadata["legacy_pipeline"] = "true"
                art.metadata["fallback_reason"] = str(e)[:200]
                return art
        if kind == "pptx" and not self._use_legacy_html_pptx:
            # PPT 不加 fallback：ib_deck JSON 字段校验失败常意味着 LLM 严重跑偏，
            # legacy（让 LLM 写 pptxgenjs JS 代码）成功率也很低，不如让上层看到错误重试。
            return await self._generate_ib_pptx(
                llm=llm,
                brief=brief,
                extra_instructions=extra_instructions,
                artifact_id=artifact_id,
                build_dir=build_dir,
            )

        return await self._generate_via_default_pipeline(
            llm=llm,
            kind=kind,
            brief=brief,
            extra_instructions=extra_instructions,
            artifact_id=artifact_id,
            build_dir=build_dir,
        )

    # ─────────────────────────────────────────────────────────────────────
    # 默认 / legacy 流水线：LLM → 剥围栏 → exec_for_kind → 产物
    # 用于 word / xlsx / markdown / txt / pdf；也作为 HTML/PPT 的 legacy 回滚。
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_via_default_pipeline(
        self,
        *,
        llm: LLMPort,
        kind: str,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
    ) -> GeneratedArtifact:
        sys_prompt = get_skill_prompt(kind, legacy=self._use_legacy_html_pptx)
        llm_out = await self._call_llm(llm, sys_prompt, brief, extra_instructions)
        code = _strip_code_fence(llm_out.content, text_mode=kind in _TEXT_KINDS)

        ext = _CANONICAL_EXT[kind]
        result = await self._exec_for_kind(kind, code, build_dir, ext)
        if not result.success or result.output_path is None:
            raise SkillError(f"skill {kind} execution failed: {result.stderr[:400]}")
        output_path = result.output_path

        title = _make_title(brief)
        metadata: dict[str, str] = {
            "kind": kind,
            "model": llm_out.model,
            "exec_elapsed_s": f"{result.elapsed_s:.2f}",
            "code_size": str(len(code)),
        }
        if self._use_legacy_html_pptx and kind in {"html", "pptx"}:
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

        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=kind,
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for(ext),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_out.latency_ms + result.elapsed_s * 1000.0,
            model=llm_out.model,
            metadata=metadata,
        )

    # ─────────────────────────────────────────────────────────────────────
    # phase4-doc-skills：HTML one-pager（Kami warm-parchment）
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_html_one_pager(
        self,
        *,
        llm: LLMPort,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
    ) -> GeneratedArtifact:
        """LLM 直出 Kami warm-parchment 整篇 HTML，10 invariants 校验后落盘。

        失败路径：
        - 含日文片假名 → ``SkillError("LLM 输出含日文片假名")``
        - invariants 不满足（rgba / emoji / 没 #f5f4ed / SVG < 3 等）→ ``SkillError("invariant 违反: ...")``

        失败信号交由上层（API / Workspace use_case）决定是重试还是降级回 legacy。
        """
        sys_prompt = get_skill_prompt("html", legacy=False)
        llm_out = await self._call_llm(llm, sys_prompt, brief, extra_instructions)
        html = _strip_code_fence(llm_out.content, text_mode=True)
        html = _extract_html_document(html)

        violations = _check_html_one_pager_invariants(html)
        if violations:
            raise SkillError(f"HTML one-pager invariant 违反: {'; '.join(violations[:3])}")

        output_path = build_dir / "output.html"
        output_path.write_text(html, encoding="utf-8")

        title = _make_title(brief)
        metadata: dict[str, str] = {
            "kind": "html",
            "model": llm_out.model,
            "exec_elapsed_s": "0.00",
            "code_size": str(len(html)),
            "skill_variant": "kami_one_pager",
            "chars": str(len(html)),
            "svg_count": str(html.lower().count("<svg")),
            "has_parchment": "true",
        }
        self._write_meta(build_dir, title=title, kind="html", ext="html")

        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="html",
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for("html"),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_out.latency_ms,
            model=llm_out.model,
            metadata=metadata,
        )

    # ─────────────────────────────────────────────────────────────────────
    # phase4-doc-skills：14 页投行风 PPT（ib_master + docxtemplater）
    # ─────────────────────────────────────────────────────────────────────

    async def _generate_ib_pptx(
        self,
        *,
        llm: LLMPort,
        brief: str,
        extra_instructions: str | None,
        artifact_id: str,
        build_dir: Path,
    ) -> GeneratedArtifact:
        """LLM 出 27 字段 JSON → ``node render.mjs ib_master.pptx data.json output.pptx``。

        步骤：
        1. 调 LLM 取 JSON 文本（system prompt = PPT_IB_DECK_SYSTEM）
        2. 预检日文片假名（M2.7 偶发，必须拒绝；INTEGRATE_PROMPT 6.6）
        3. 提取 + 解析 JSON，校验 27 个字段全部为非空 string
        4. 写 data.json 到 build_dir，串行调 node render.mjs（不并发，避免 npm 锁）
        5. 验证产物存在且 > 8KB，返回 GeneratedArtifact
        """
        if not _PPT_IB_MASTER.exists() or not _PPT_IB_RENDER_MJS.exists():
            raise SkillError(
                "ppt_ib_deck assets 缺失，期望在 "
                f"{_PPT_IB_DECK_DIR}（ib_master.pptx + render.mjs）。"
                "请在 backend/app/adapters/skill/assets/ppt_ib_deck/ 跑 npm install。"
            )

        sys_prompt = get_skill_prompt("pptx", legacy=False)
        llm_out = await self._call_llm(llm, sys_prompt, brief, extra_instructions)
        raw = llm_out.content

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
            "model": llm_out.model,
            "exec_elapsed_s": f"{render_result.elapsed_s:.2f}",
            "skill_variant": "ib_deck_v3",
            "field_count": str(len(_PPT_IB_DECK_FIELDS)),
            "slide_count_hint": "14",
            "code_size": str(len(raw)),
        }
        self._write_meta(build_dir, title=title, kind="pptx", ext="pptx")

        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="pptx",
            title=title,
            file_path=str(output_path),
            mime_type=_mime_for("pptx"),
            size_bytes=output_path.stat().st_size,
            generation_latency_ms=llm_out.latency_ms + render_result.elapsed_s * 1000.0,
            model=llm_out.model,
            metadata=metadata,
        )

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

    async def _call_llm(
        self,
        llm: LLMPort,
        sys_prompt: str,
        brief: str,
        extra_instructions: str | None,
    ) -> _LLMOutput:
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
        return _LLMOutput(
            content=resp.content,
            model=resp.model,
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
