"""门面函数 generate_minutes — 串起 extractor + renderers + I/O。

这是模块的公开 API 入口。被 cli.py、demo/server.py、未来的 EchoBridgeClient 调用。

设计原则：
- 调用方提供 LLMClient 实例（依赖反转）
- 任何降级都不静默：所有 warnings 收集到 MinutesResult.warnings 返回
- 任何失败都先尝试落 data.json（已有数据就先存档），再尝试 HTML/docx
"""
from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger

from minutes_kit.extractor import ExtractorError, extract_minutes
from minutes_kit.llm_client import LLMClient
from minutes_kit.models import MinutesResult, TranscriptTurn
from minutes_kit.renderers.docx import render_docx
from minutes_kit.renderers.html import render_html


class MinutesGenerationError(RuntimeError):
    """整个流程不可恢复的失败（如 extractor 抛 ExtractorError）。"""


async def generate_minutes(
    transcript: list[TranscriptTurn] | list[dict],
    *,
    llm_client: LLMClient,
    out_dir: Path,
    participants: list[str] | None = None,
    title_hint: str | None = None,
    minutes_id: str | None = None,
    use_claude_skill: bool = True,
    inline_mermaid_js: bool = True,
) -> MinutesResult:
    """完整流程：转录 → 正典 JSON → preview.html + minutes.docx + flow.png + data.json。

    Args:
        transcript: TranscriptTurn 列表，或可被 TranscriptTurn.from_dict 解析的 dict 列表
        llm_client: 依赖注入的 LLM 客户端（OpenAIClient / EchoBridgeClient / mock）
        out_dir: 产物目录，会自动创建
        participants: 显式指定参会人；不传则从 transcript 推断
        title_hint: 会议标题提示；不传由 LLM 自生成
        minutes_id: 唯一 ID；不传自动生成 12 字符 hex
        use_claude_skill: docx 是否走 Claude 主路径（False 直接 fallback）
        inline_mermaid_js: HTML 是否内联 mermaid.min.js（True = 离线可用）

    Returns:
        MinutesResult，含 data + 产物路径 + warnings

    Raises:
        MinutesGenerationError: extractor 失败、out_dir 不可写等不可恢复错误
    """
    # 1. 输入规整：dict → TranscriptTurn
    turns = _normalize_transcript(transcript)
    if not turns:
        raise MinutesGenerationError("transcript 为空或全部无效")

    # 2. 准备 out_dir
    out_dir = Path(out_dir).expanduser().resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MinutesGenerationError(f"无法创建 out_dir {out_dir}: {exc}") from exc

    mid = minutes_id or uuid.uuid4().hex[:12]
    logger.info(
        f"[generate_minutes] start minutes_id={mid} turns={len(turns)} out_dir={out_dir}"
    )

    # 3. extractor：3 节点 LLM 编排
    try:
        data = await extract_minutes(
            turns,
            llm_client=llm_client,
            participants=participants,
            title_hint=title_hint,
            minutes_id=mid,
        )
    except ExtractorError as exc:
        raise MinutesGenerationError(f"提取失败: {exc}") from exc

    # 4. 落 data.json（即便后面 HTML/docx 渲染失败，JSON 已存档）
    data_json_path = out_dir / "data.json"
    data.write_json(data_json_path)
    logger.info(f"[generate_minutes] data.json written ({data_json_path.stat().st_size} bytes)")

    # 5. 渲染 HTML（同步快，几十毫秒）
    preview_html_path = out_dir / "preview.html"
    try:
        render_html(data, preview_html_path, inline_mermaid_js=inline_mermaid_js)
    except Exception as exc:
        logger.error(f"[generate_minutes] HTML 渲染失败: {exc}")
        # 不抛——已经有 data.json 兜底，调用方可以决定怎么办
        preview_html_path = out_dir / "preview.html"  # 保留路径占位

    # 6. 渲染 docx（可能耗时）
    docx_path = out_dir / "minutes.docx"
    docx_result = await render_docx(
        data,
        docx_path,
        use_claude_skill=use_claude_skill,
    )

    return MinutesResult(
        data=data,
        out_dir=out_dir,
        data_json_path=data_json_path,
        preview_html_path=preview_html_path,
        docx_path=docx_result.docx_path,
        flow_png_path=docx_result.flow_png_path,
        docx_generator=docx_result.generator,
        warnings=docx_result.warnings,
    )


def _normalize_transcript(
    transcript: list[TranscriptTurn] | list[dict],
) -> list[TranscriptTurn]:
    """允许传 dict 列表，自动转 TranscriptTurn；过滤空文本。"""
    out: list[TranscriptTurn] = []
    for item in transcript:
        if isinstance(item, TranscriptTurn):
            turn = item
        elif isinstance(item, dict):
            turn = TranscriptTurn.from_dict(item)
        else:
            continue
        if turn.text.strip():
            out.append(turn)
    return out
