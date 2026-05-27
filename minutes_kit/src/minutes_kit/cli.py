"""minutes_kit 命令行入口。

用法：
    python -m minutes_kit.cli \\
        --transcript demo/sample_transcripts/meetly_demo.txt \\
        --out ./out/run_001/ \\
        --participants "A,B,C" \\
        --title-hint "周三例会"

退出码：
    0 全部成功
    1 部分降级（如 docx 走了 fallback、流程图 PNG 缺失）
    2 不可恢复失败（提取失败 / out_dir 不可写 / transcript 为空）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from loguru import logger

from minutes_kit.llm_client import OpenAIClient
from minutes_kit.orchestrator import MinutesGenerationError, generate_minutes
from minutes_kit.transcript_io import load_transcript


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="minutes_kit",
        description="把会议转录精修成 HTML 预览 + Word 文档 + Mermaid 流程图",
    )
    p.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="转录文件路径 (.txt / .json / .jsonl)",
    )
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="产物输出目录（自动创建）",
    )
    p.add_argument(
        "--participants",
        type=str,
        default="",
        help="逗号分隔的参会人列表；不传则从转录推断",
    )
    p.add_argument(
        "--title-hint",
        type=str,
        default=None,
        help="会议标题提示；不传由 LLM 自生成",
    )
    p.add_argument(
        "--llm",
        type=str,
        default="openai",
        choices=["openai"],
        help="LLM 后端选择（目前仅 openai；未来 echo-bridge）",
    )
    p.add_argument(
        "--model",
        type=str,
        default=None,
        help="覆盖 LLM 模型名；不传走 OpenAIClient 默认（MINUTES_KIT_MODEL 或 gpt-4o-mini）",
    )
    p.add_argument(
        "--no-claude",
        action="store_true",
        help="跳过 Claude skill 主路径，docx 直接走 python-docx 兜底",
    )
    p.add_argument(
        "--no-inline-mermaid",
        action="store_true",
        help="HTML 不内联 mermaid.min.js，改用 CDN 加载（依赖网络）",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出 DEBUG 日志",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()

    # 配置日志
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[minutes_kit] 用户中断", file=sys.stderr)
        return 130


async def _run(args: argparse.Namespace) -> int:
    # 1. 读 transcript
    if not args.transcript.exists():
        logger.error(f"transcript 文件不存在: {args.transcript}")
        return 2
    try:
        turns = load_transcript(args.transcript)
    except Exception as exc:
        logger.error(f"transcript 解析失败: {exc}")
        return 2
    if not turns:
        logger.error("transcript 解析后为空，无法生成纪要")
        return 2
    logger.info(f"transcript 解析完成：{len(turns)} 条 turn")

    # 2. 参会人
    participants = [p.strip() for p in args.participants.split(",") if p.strip()] or None

    # 3. LLM 客户端
    if args.llm == "openai":
        llm = OpenAIClient(model=args.model)
        base = getattr(llm, "base_url", "?")
        model = getattr(llm, "model", "?")
        logger.info(f"LLM = OpenAIClient base_url={base} model={model}")
    else:
        logger.error(f"未知 --llm: {args.llm}")
        return 2

    # 4. 跑
    t0 = time.monotonic()
    try:
        result = await generate_minutes(
            transcript=turns,
            llm_client=llm,
            out_dir=args.out,
            participants=participants,
            title_hint=args.title_hint,
            use_claude_skill=not args.no_claude,
            inline_mermaid_js=not args.no_inline_mermaid,
        )
    except MinutesGenerationError as exc:
        logger.error(f"生成失败: {exc}")
        return 2
    elapsed_s = time.monotonic() - t0

    # 5. 报告产物
    print("\n" + "=" * 60)
    print(f"  会议纪要生成完成 (耗时 {elapsed_s:.1f}s)")
    print("=" * 60)
    print(f"  标题:        {result.data.title}")
    print(f"  决议:        {len(result.data.decisions)} 条")
    print(f"  待办:        {len(result.data.todos)} 条")
    print(f"  话题:        {len(result.data.topics)} 个")
    print(f"  流程图:      {result.data.flow_kind} ({len(result.data.flow_mermaid)} 字符)")
    print()
    print(f"  data.json:   {result.data_json_path}")
    print(f"  preview.html:{result.preview_html_path}  ← 双击打开看效果")
    if result.docx_path:
        print(f"  minutes.docx:{result.docx_path}  ({result.docx_generator})")
    else:
        print("  minutes.docx: (失败)")
    if result.flow_png_path:
        print(f"  flow.png:    {result.flow_png_path}")
    print()
    if result.warnings:
        print("  ⚠ 警告:")
        for w in result.warnings:
            print(f"    - {w}")
        print()
    print("=" * 60)

    # 退出码
    if result.docx_path is None:
        return 1  # 部分降级
    if result.docx_generator == "python_fallback" or not result.flow_png_path:
        return 1  # 部分降级
    return 0


if __name__ == "__main__":
    sys.exit(main())
