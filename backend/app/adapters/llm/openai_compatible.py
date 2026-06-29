"""OpenAI 兼容 LLM adapter（主通道 / 可选 fast 通道 + 流式 + 重试）。

设计参考 echo backend/app/llm.py 的双通道架构（FAST/MAIN）：
- FAST 通道：默认跟随 MAIN；私有部署可切到自定义 vLLM
- MAIN 通道：复杂任务、长生成；max_tokens=80000

约定（用户决策 2026-05-26）：
- thinking-only 模型必须用 80k+ max_tokens（M2.7 / Qwen3 / GLM-5），否则 reasoning 段吃光预算 → content=""
- Qwen3 reasoning 模型默认关 enable_thinking（实时场景不要思考链泄漏）
- 部分 OpenAI 兼容代理会忽略 enable_thinking=False → 后处理剥 ``<think>...</think>``
- 任何失败抛 LLMError，不静默兜底；上层决定是否切 fallback
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIError, APITimeoutError, AsyncOpenAI

from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage

_REASONING_HINTS = ("Qwen3", "qwen3", "GLM-5", "glm-5", "DeepSeek-R1", "M2.7", "MiniMax")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"^.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _is_reasoning(model: str) -> bool:
    return any(h in model for h in _REASONING_HINTS)


def _strip_thinking(text: str) -> str:
    """剥 ``<think>...</think>`` 段，兼容 thinking-only 模型 + OpenAI 兼容代理。

    - 完整闭合：直接删除整段
    - 仅有 ``</think>`` 闭合（开头标签被截）：取闭合之后的内容
    - 完全没有：原样返回
    """
    if "</think>" not in text.lower():
        return text
    if "<think>" in text.lower():
        return _THINK_BLOCK_RE.sub("", text).strip()
    return _THINK_OPEN_RE.sub("", text, count=1).strip()


class _ThinkStripper:
    """Stream 状态机：吞掉 ``<think>...</think>`` 段。

    设计简化：模型几乎总是以 ``<think>`` 开场（甚至首个 delta 就是 ``<think>``），
    因此用 buffer 累计直到看到 ``</think>``，之后正常透传。
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""
        self._first = True

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        if self._first:
            self._first = False
            stripped = self._buf.lstrip()
            if stripped.lower().startswith("<think>"):
                self._in_think = True
                self._buf = stripped
        if self._in_think:
            close = self._buf.lower().find("</think>")
            if close == -1:
                return ""
            after = self._buf[close + len("</think>") :].lstrip()
            self._buf = ""
            self._in_think = False
            return after
        # 非 thinking 模式：直接吐出 buffer
        out = self._buf
        self._buf = ""
        return out

    def flush(self) -> str:
        if self._in_think:
            return ""
        out = self._buf
        self._buf = ""
        return out


class LLMError(RuntimeError):
    """LLM 调用失败（网络/超时/上游 5xx）。"""


class OpenAICompatibleLLM:
    """实现 ports.llm.LLMPort 的 OpenAI 兼容客户端。

        路由：根据 ``model`` 参数决定走 MAIN 还是 FAST。
    无 model 时按对应通道默认模型。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(trust_env=False, timeout=600.0)
        self._main = AsyncOpenAI(
            api_key=settings.yunwu_open_key or "EMPTY",
            base_url=settings.llm_main_base_url,
            http_client=self._http,
        )
        self._fast = AsyncOpenAI(
            api_key=settings.llm_local_api_key or "EMPTY",
            base_url=settings.llm_fast_base_url,
            http_client=self._http,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _pick(self, model: str | None) -> tuple[AsyncOpenAI, str, int]:
        s = self._settings
        if model is None:
            return self._main, s.llm_main_model, s.llm_main_max_tokens
        if model == s.llm_fast_model:
            if s.llm_fast_model == s.llm_main_model and s.llm_fast_base_url.rstrip(
                "/"
            ) == s.llm_main_base_url.rstrip("/"):
                return self._main, s.llm_main_model, s.llm_fast_max_tokens
            return self._fast, s.llm_fast_model, s.llm_fast_max_tokens
        if model == s.llm_main_model:
            return self._main, s.llm_main_model, s.llm_main_max_tokens
        if model in {s.llm_fallback_1, s.llm_fallback_2}:
            return self._main, model, s.llm_main_max_tokens
        return self._main, model, s.llm_main_max_tokens

    @staticmethod
    def _build_kwargs(
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
        }
        if _is_reasoning(model):
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return kwargs

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 120.0,
    ) -> LLMResponse:
        client, use_model, default_max = self._pick(model)
        effective_max = max_tokens if max_tokens is not None else default_max
        kwargs = self._build_kwargs(use_model, messages, effective_max, temperature, stream=False)

        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(**kwargs), timeout=timeout_s
            )
        except (TimeoutError, APITimeoutError) as e:
            raise LLMError(f"{use_model} timeout after {timeout_s}s") from e
        except APIError as e:
            raise LLMError(f"{use_model} api error: {e}") from e

        latency_ms = (time.monotonic() - t0) * 1000
        choice = resp.choices[0]
        content = _strip_thinking(choice.message.content or "")
        usage = LLMUsage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            total_tokens=getattr(resp.usage, "total_tokens", 0) if resp.usage else 0,
        )
        return LLMResponse(
            content=content,
            model=use_model,
            finish_reason=choice.finish_reason,
            usage=usage,
            latency_ms=latency_ms,
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 600.0,
    ) -> AsyncIterator[str]:
        client, use_model, default_max = self._pick(model)
        effective_max = max_tokens if max_tokens is not None else default_max
        kwargs = self._build_kwargs(use_model, messages, effective_max, temperature, stream=True)

        try:
            stream = await asyncio.wait_for(
                client.chat.completions.create(**kwargs), timeout=timeout_s
            )
        except (TimeoutError, APITimeoutError) as e:
            raise LLMError(f"{use_model} stream timeout after {timeout_s}s") from e
        except APIError as e:
            raise LLMError(f"{use_model} stream api error: {e}") from e

        filt = _ThinkStripper()
        try:
            async for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                if delta and delta.content:
                    out = filt.feed(delta.content)
                    if out:
                        yield out
            tail = filt.flush()
            if tail:
                yield tail
        finally:
            await stream.close()
