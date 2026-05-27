"""Claude Code subprocess backend（移植自 meetly _claude_code.py）。

启动假设
--------
- 本机已安装 ``claude`` CLI（``which claude`` 能找到，``npm i -g @anthropic-ai/claude-code``）
- ``ANTHROPIC_API_KEY`` 已配置；或同网段有 m27 / anthropic-proxy 服务可指向

去除：
- yoli_agent.Tool 依赖（本模块不引入 yoli_agent）
- meetly 项目特定的 ``vendor skills`` 默认搜索路径（改为显式从环境变量读）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from minutes_kit._harness_events import (
    AssistantTextEvent,
    HarnessEvent,
    ResultEvent,
    SystemEvent,
    ToolResultEvent,
    ToolUseEvent,
)

logger = logging.getLogger("minutes_kit.claude_backend")


@dataclass(slots=True)
class ClaudeCodeConfig:
    """Claude Code subprocess 配置。"""

    proxy_base_url: str | None = None  # 留空 = 走 ANTHROPIC_API_KEY 真路径
    model: str | None = None  # 留空 = 让 claude 自己定
    auth_token: str | None = None
    binary: str = "claude"
    timeout_s: float = 1800.0
    dangerously_skip_permissions: bool = True
    include_partial_messages: bool = True
    plugin_dirs: tuple[str, ...] = ()


class ClaudeCodeBackend:
    """通用 harness 后端：一句 prompt 进去，一串事件出来，最后一个总结。"""

    name = "claude_code"

    def __init__(self, config: ClaudeCodeConfig | None = None) -> None:
        self.cfg = config or ClaudeCodeConfig()
        if shutil.which(self.cfg.binary) is None:
            raise RuntimeError(
                f"找不到 {self.cfg.binary!r} 可执行文件；"
                "先 `npm install -g @anthropic-ai/claude-code`"
            )

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # 绕开本机系统代理（macOS Clash TUN 会把 0/0 路由抢走）
        env.setdefault("HTTP_PROXY", "")
        env.setdefault("HTTPS_PROXY", "")
        env.setdefault("NO_PROXY", "*")

        if self.cfg.proxy_base_url:
            env["ANTHROPIC_BASE_URL"] = self.cfg.proxy_base_url
            env["ANTHROPIC_AUTH_TOKEN"] = self.cfg.auth_token or "dummy"
            env["ANTHROPIC_API_KEY"] = ""  # 让 AUTH_TOKEN 生效

        if self.cfg.model:
            env["ANTHROPIC_MODEL"] = self.cfg.model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = self.cfg.model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = self.cfg.model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = self.cfg.model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = self.cfg.model

        env["API_TIMEOUT_MS"] = str(int(self.cfg.timeout_s * 1000))
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _build_args(self, system_prompt: str | None = None) -> list[str]:
        args = [
            self.cfg.binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.cfg.include_partial_messages:
            args.append("--include-partial-messages")
        if self.cfg.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        if self.cfg.plugin_dirs:
            args.append("--plugin-dir")
            args.extend(self.cfg.plugin_dirs)
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
        return args

    async def run(
        self,
        prompt: str,
        *,
        workspace_dir: str | None = None,
        timeout_s: float | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        wall_timeout = timeout_s or self.cfg.timeout_s
        cwd = workspace_dir or os.getcwd()

        logger.info("spawn claude in %s (timeout=%ss)", cwd, wall_timeout)
        # StreamReader 默认 64KB；产 PPT/embed base64/长 HTML 时单行可能几百 KB
        proc = await asyncio.create_subprocess_exec(
            *self._build_args(system_prompt=system_prompt),
            cwd=cwd,
            env=self._build_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )

        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        try:
            async for event in self._read_events(proc, wall_timeout):
                yield event
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def _read_events(
        self, proc: asyncio.subprocess.Process, wall_timeout: float
    ) -> AsyncIterator[HarnessEvent]:
        assert proc.stdout is not None
        deadline = asyncio.get_event_loop().time() + wall_timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning("claude wall timeout reached, killing")
                proc.kill()
                raise TimeoutError(f"claude_code wall timeout {wall_timeout}s exceeded")
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except TimeoutError:
                proc.kill()
                raise
            except ValueError as exc:
                # buffer 溢出（16MB 仍可能）：Claude 此时往往已把产物写进 workspace，
                # 跳过当前行继续读，不让单行解析失败拖垮整个任务
                logger.warning("stream-json line overflow, skipping: %s", exc)
                with contextlib.suppress(asyncio.IncompleteReadError, ValueError):
                    await proc.stdout.readuntil(b"\n")
                continue

            if not line:
                break

            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.debug("non-json stdout line dropped: %r", line[:160])
                continue

            for evt in self._translate(obj):
                yield evt
                if isinstance(evt, ResultEvent):
                    return

    @staticmethod
    def _translate(obj: dict[str, Any]) -> list[HarnessEvent]:
        """单条 Claude Code stream-json 事件 → 0..N 条内部事件。"""
        out: list[HarnessEvent] = []
        sid = obj.get("session_id", "")
        t = obj.get("type")

        if t == "system":
            out.append(SystemEvent(session_id=sid, payload=obj))
            return out

        if t == "result":
            out.append(
                ResultEvent(
                    session_id=sid,
                    is_error=bool(obj.get("is_error")),
                    duration_ms=int(obj.get("duration_ms", 0)),
                    num_turns=int(obj.get("num_turns", 0)),
                    result_text=str(obj.get("result", "")),
                    raw=obj,
                )
            )
            return out

        if t == "assistant":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text" and (text := block.get("text")):
                    out.append(AssistantTextEvent(session_id=sid, text=text, stream=False))
                elif btype == "tool_use":
                    out.append(
                        ToolUseEvent(
                            session_id=sid,
                            tool_use_id=block.get("id", ""),
                            name=block.get("name", ""),
                            input=block.get("input") or {},
                        )
                    )
            return out

        if t == "user":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, list):
                        text = "".join(
                            b.get("text", "")
                            for b in raw
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        text = str(raw or "")
                    out.append(
                        ToolResultEvent(
                            session_id=sid,
                            tool_use_id=block.get("tool_use_id", ""),
                            output=text,
                            is_error=bool(block.get("is_error")),
                        )
                    )
            return out

        if t == "stream_event":
            evt = obj.get("event") or {}
            if evt.get("type") == "content_block_delta":
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta" and (text := delta.get("text")):
                    out.append(AssistantTextEvent(session_id=sid, text=text, stream=True))
            return out

        return out
