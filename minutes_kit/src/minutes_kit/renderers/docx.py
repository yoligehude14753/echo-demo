"""docx 渲染器：MeetingMinutesData → minutes.docx。

双轨：
1. **主路径**：Claude Code subprocess + Anthropic docx skill
   - 流程图先渲成 PNG（mmdc / mermaid.ink）再让 claude 嵌入
   - 需要本机装了 ``claude`` binary + Anthropic docx skill
2. **兜底**：python-docx
   - 永远可用，无外部依赖
   - 视觉品质比 skill 弱，但结构完整能用

策略：try claude → 产物缺失/异常 → 自动 fallback。返回值带 `generator` 字段标识用了哪条路径。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from minutes_kit._fallback_office import fallback_docx
from minutes_kit.mermaid_render import render_mermaid_to_png
from minutes_kit.models import MeetingMinutesData

Generator = Literal["claude", "python_fallback", "skipped"]


@dataclass(slots=True)
class DocxRenderResult:
    docx_path: Path | None
    flow_png_path: Path | None
    generator: Generator
    warnings: list[str]


async def render_docx(
    data: MeetingMinutesData,
    out_path: Path,
    *,
    use_claude_skill: bool = True,
    claude_timeout_s: float = 900.0,
    flow_render_timeout_s: float = 12.0,
) -> DocxRenderResult:
    """生成 docx 文档。

    Args:
        data: 正典数据
        out_path: 目标 docx 路径，如 .../minutes.docx
        use_claude_skill: 是否尝试 Claude Code skill 主路径（False 直接走 fallback）
        claude_timeout_s: Claude subprocess 总超时
        flow_render_timeout_s: Mermaid PNG 渲染超时

    Returns:
        DocxRenderResult 含产物路径 + 用了哪条路径
    """
    warnings: list[str] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = out_path.parent

    # Step 1: 渲流程图 PNG（两条路径都要嵌图）
    flow_png_path: Path | None = None
    if data.flow_mermaid:
        png_target = workspace / "flow.png"
        try:
            flow_png_path = await render_mermaid_to_png(
                data.flow_mermaid,
                png_target,
                timeout_s=flow_render_timeout_s,
            )
            if flow_png_path is None:
                warnings.append("流程图未渲染：mmdc 和 mermaid.ink 均不可用")
        except Exception as exc:
            logger.warning(f"[docx] flow PNG 渲染异常: {exc}")
            warnings.append(f"流程图渲染异常: {exc}")

    # Step 2: 主路径 Claude skill
    if use_claude_skill:
        ok = await _try_claude_path(data, out_path, flow_png_path, claude_timeout_s, warnings)
        if ok:
            return DocxRenderResult(
                docx_path=out_path,
                flow_png_path=flow_png_path,
                generator="claude",
                warnings=warnings,
            )

    # Step 3: 兜底 python-docx
    try:
        _try_python_fallback(data, out_path, flow_png_path)
        return DocxRenderResult(
            docx_path=out_path,
            flow_png_path=flow_png_path,
            generator="python_fallback",
            warnings=warnings,
        )
    except Exception as exc:
        logger.error(f"[docx] python-docx fallback 也失败了: {exc}")
        warnings.append(f"python-docx fallback 失败: {exc}")
        return DocxRenderResult(
            docx_path=None,
            flow_png_path=flow_png_path,
            generator="skipped",
            warnings=warnings,
        )


# ── 主路径 ─────────────────────────────────────────────────────────────


async def _try_claude_path(
    data: MeetingMinutesData,
    out_path: Path,
    flow_png_path: Path | None,
    timeout_s: float,
    warnings: list[str],
) -> bool:
    """尝试 Claude Code + docx skill。返回 True = 产物落地成功。"""
    try:
        # 检测 binary 可用性（lazy，避免在测试环境硬卡）
        from minutes_kit._claude_runner import get_backend, run_with_skill

        get_backend()
    except Exception as exc:
        logger.info(f"[docx] Claude 主路径不可用，跳过: {exc}")
        warnings.append(f"Claude skill 不可用: {exc}")
        return False

    prompt = _build_claude_prompt(data, out_path.name, flow_png_path)
    try:
        result = await run_with_skill(
            prompt=prompt,
            workspace_dir=out_path.parent,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        logger.warning(f"[docx] Claude subprocess 异常: {exc}")
        warnings.append(f"Claude subprocess 异常: {exc}")
        return False

    if result.get("is_error"):
        warnings.append(f"Claude 返回 is_error=True: {result.get('result_text', '')[:200]}")
        # 不立刻返回 False，因为有时 claude 把产物写出来了也会报 error
    if not out_path.exists() or out_path.stat().st_size < 1024:
        warnings.append("Claude 退出后 docx 文件不存在或过小，将走 fallback")
        return False

    logger.info(
        f"[docx] Claude 主路径成功 duration={result.get('duration_ms')}ms "
        f"size={out_path.stat().st_size}"
    )
    return True


def _build_claude_prompt(
    data: MeetingMinutesData,
    target_filename: str,
    flow_png_path: Path | None,
) -> str:
    """给 Claude 的 prompt — 让它用 docx skill 出高质量 Word。"""
    flow_section = ""
    if flow_png_path and flow_png_path.exists():
        flow_section = (
            f'\n4a. "会议流程" 章节 — 嵌入图片 `{flow_png_path.name}`'
            f'（用 docx skill 的 InsertPicture API；宽度约 15cm）'
        )

    decisions_json = json.dumps(
        [d.to_dict() for d in data.decisions], ensure_ascii=False, indent=2
    )[:6000]
    todos_json = json.dumps(
        [t.to_dict() for t in data.todos], ensure_ascii=False, indent=2
    )[:4000]
    topics_json = json.dumps(
        [t.to_dict() for t in data.topics], ensure_ascii=False, indent=2
    )[:4000]

    return f"""你的任务：用 Anthropic docx skill 生成一份会议纪要 Word 文档，写到 `{target_filename}`。

# 文档结构（按顺序）
1. 一级标题：{data.title}
2. 元信息段（斜体）：时间 + 参会人
3. "摘要" 二级标题 + 一段 abstract{flow_section}
4. "会议决议" 二级标题 + 编号列表（每条 statement 加粗 + 灰色字依据 + 蓝色字影响）
5. "待办事项" 二级标题 + 真实 docx 表格（4 列：任务/负责人/截止/优先级）
6. "话题展开" 二级标题 + 每个 topic 一个三级标题，下面列要点
7. "完整纪要" 二级标题 + 把 summary_md 转换为对应样式（保留 ## ### 层级，列表用 bullet）

# 输入数据

## title
{data.title}

## abstract
{data.abstract}

## 元信息
- 时间：{data.from_time} ~ {data.to_time}
- 参会人：{', '.join(data.participants)}

## decisions (JSON)
{decisions_json}

## todos (JSON, priority 用「高」「中」「低」中文标签)
{todos_json}

## topics (JSON)
{topics_json}

## summary_md
{data.summary_md[:6000]}

# 工程要求
- 用 docx-js 或 python-docx 生成
- 字体默认（不要自定义中文字体，让系统选）
- 一级标题、二级标题、三级标题用 Heading 1/2/3 样式
- 待办表格用真实 docx table（不是 markdown）
- 生成完毕立刻停（不要写 README，不要 cat 文件，不要给我解释）
"""


# ── 兜底路径 ──────────────────────────────────────────────────────────


def _try_python_fallback(
    data: MeetingMinutesData,
    out_path: Path,
    flow_png_path: Path | None,
) -> None:
    fallback_docx(
        target_path=out_path,
        title=data.title,
        abstract=data.abstract,
        summary_md=data.summary_md,
        decisions=[d.to_dict() for d in data.decisions],
        todos=[t.to_dict() for t in data.todos],
        topics=[t.to_dict() for t in data.topics],
        flow_png_path=flow_png_path,
        participants=data.participants,
        time_range=f"{data.from_time} - {data.to_time}",
    )
    logger.info(f"[docx] python-docx fallback 写入 {out_path.name} size={out_path.stat().st_size}")
