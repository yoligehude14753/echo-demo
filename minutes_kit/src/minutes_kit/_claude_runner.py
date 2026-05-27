"""ClaudeCodeBackend 单例 + run_with_skill 辅助（移植自 meetly）。

去除：
- 对 ``get_settings()`` 的依赖（用环境变量自描述）
- 默认 vendor skills 路径（改为读 ``MINUTES_KIT_SKILLS_DIR`` 环境变量）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

from minutes_kit._claude_backend import ClaudeCodeBackend, ClaudeCodeConfig
from minutes_kit._harness_events import ResultEvent, ToolUseEvent

_backend: ClaudeCodeBackend | None = None


def get_backend() -> ClaudeCodeBackend:
    """惰性单例。环境变量驱动配置，无需调用方关心。"""
    global _backend
    if _backend is None:
        proxy = os.environ.get("MINUTES_KIT_CLAUDE_PROXY") or os.environ.get("ANTHROPIC_BASE_URL")
        model = os.environ.get("MINUTES_KIT_CLAUDE_MODEL")
        binary = os.environ.get("MINUTES_KIT_CLAUDE_BIN", "claude")
        timeout_s = float(os.environ.get("MINUTES_KIT_CLAUDE_TIMEOUT_S", "1800"))
        skills_dir = os.environ.get("MINUTES_KIT_SKILLS_DIR", "").strip()

        plugin_dirs: tuple[str, ...] = ()
        if skills_dir:
            sd = Path(skills_dir).expanduser().resolve()
            if (sd / ".claude-plugin" / "marketplace.json").is_file():
                plugin_dirs = (str(sd),)
            else:
                logger.warning(
                    f"MINUTES_KIT_SKILLS_DIR={sd} 不存在 .claude-plugin/marketplace.json，"
                    "将不向 claude 注入 skills 插件目录"
                )

        _backend = ClaudeCodeBackend(
            ClaudeCodeConfig(
                proxy_base_url=proxy,
                model=model,
                binary=binary,
                timeout_s=timeout_s,
                plugin_dirs=plugin_dirs,
            )
        )
    return _backend


def reset_backend_for_tests() -> None:
    """测试钩子：清单例（避免环境变量改了 backend 还旧）。"""
    global _backend
    _backend = None


async def run_with_skill(
    *,
    prompt: str,
    workspace_dir: Path,
    system_prompt: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """跑一次 Claude Code subprocess，产物落到 ``workspace_dir``。

    返回:
        {result_text, is_error, duration_ms, num_turns, tool_uses}

    调用方通常不读 result_text，只看 ``workspace_dir`` 里是否多了期望的文件。
    """
    backend = get_backend()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    result_text = ""
    is_error = False
    duration_ms = 0
    num_turns = 0
    tool_uses: list[dict[str, Any]] = []

    try:
        async for event in backend.run(
            prompt=prompt,
            workspace_dir=str(workspace_dir),
            timeout_s=timeout_s,
            system_prompt=system_prompt,
        ):
            if isinstance(event, ToolUseEvent):
                tool_uses.append({"name": event.name, "input_keys": list(event.input.keys())})
            elif isinstance(event, ResultEvent):
                result_text = event.result_text
                is_error = event.is_error
                duration_ms = event.duration_ms
                num_turns = event.num_turns
    except Exception as exc:
        logger.warning(f"claude_code run failed: {exc}")
        is_error = True
        result_text = str(exc)

    return {
        "result_text": result_text,
        "is_error": is_error,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
        "tool_uses": tool_uses,
    }
