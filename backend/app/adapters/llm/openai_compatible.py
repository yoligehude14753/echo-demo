"""OpenAI 兼容 LLM adapter（Yunwu / heyi-local Qwen 双路由 + 流式 + 重试）。

设计参考 echo backend/app/llm.py 的双通道架构（FAST/MAIN）：
- FAST 通道：Qwen3-1.7B on heyi-bj :7860 → 用于路由、短问答、纯结构化抽取
- MAIN 通道：Yunwu MiniMax-M2.7 → 复杂任务、长生成；max_tokens=80000

约定（用户决策 2026-05-26）：
- thinking-only 模型必须用 80k+ max_tokens（M2.7 / Qwen3 / GLM-5），否则 reasoning 段吃光预算 → content=""
- Qwen3 reasoning 模型默认关 enable_thinking（实时场景不要思考链泄漏）
- 任何失败抛 LLMError，不静默兜底；上层决定是否切 fallback
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIError, APITimeoutError, AsyncOpenAI

from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage

_REASONING_HINTS = ("Qwen3", "qwen3", "GLM-5", "glm-5", "DeepSeek-R1", "M2.7", "MiniMax")


def _is_reasoning(model: str) -> bool:
    return any(h in model for h in _REASONING_HINTS)


class LLMError(RuntimeError):
    """LLM 调用失败（网络/超时/上游 5xx）。"""


class OpenAICompatibleLLM:
    """实现 ports.llm.LLMPort 的 OpenAI 兼容客户端。

    路由：根据 ``model`` 参数决定走 MAIN(Yunwu) 还是 FAST(heyi-local)。
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
        if model is None or model == s.llm_main_model:
            return self._main, s.llm_main_model, s.llm_main_max_tokens
        if model == s.llm_fast_model:
            return self._fast, s.llm_fast_model, s.llm_fast_max_tokens
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
        content = choice.message.content or ""
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

        try:
            async for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        finally:
            await stream.close()
