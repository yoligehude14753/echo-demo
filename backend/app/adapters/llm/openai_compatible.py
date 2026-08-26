"""Echo LLM Port backed by the public yoli_llm model-gateway client."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator

from yoli_llm.model_gateway import (
    GatewayResult,
    ModelGatewayClient,
    ModelGatewayError,
)

from app.adapters.llm.capability_resolver import resolve_chat_capability
from app.adapters.llm.model_gateway_factory import create_model_gateway_client
from app.config import Settings
from app.ports.llm import LLMPriority
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor, QuotaReservation

_REASONING_HINTS = ("Qwen3", "qwen3", "GLM-5", "M2.7", "MiniMax")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"^.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_CHAT_FRAMING_TOKEN_RESERVE = 256
_CHAT_MESSAGE_TOKEN_RESERVE = 16
logger = logging.getLogger("echodesk.llm")


class _ModelRequestLease:
    def __init__(self, scheduler: _ModelRequestScheduler, *, background: bool) -> None:
        self._scheduler = scheduler
        self._background = background
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._scheduler.release(background=self._background)


class _ModelRequestScheduler:
    """Bound model concurrency while giving interactive work the next free slot."""

    def __init__(self, *, max_concurrent: int = 2, max_background: int = 1) -> None:
        self._max_concurrent = max_concurrent
        self._max_background = max_background
        self._condition = asyncio.Condition()
        self._active = 0
        self._active_background = 0
        self._foreground_waiters = 0

    async def acquire(self, priority: LLMPriority) -> _ModelRequestLease:
        background = priority == "background"
        acquired = False
        async with self._condition:
            if not background:
                self._foreground_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: self._active < self._max_concurrent
                    and (
                        not background
                        or (
                            self._foreground_waiters == 0
                            and self._active_background < self._max_background
                        )
                    )
                )
                if not background:
                    self._foreground_waiters -= 1
                self._active += 1
                if background:
                    self._active_background += 1
                acquired = True
            finally:
                if not background and not acquired:
                    self._foreground_waiters -= 1
                    self._condition.notify_all()
        return _ModelRequestLease(self, background=background)

    async def release(self, *, background: bool) -> None:
        async with self._condition:
            self._active -= 1
            if background:
                self._active_background -= 1
            self._condition.notify_all()


def _is_reasoning(model: str) -> bool:
    return any(hint in model for hint in _REASONING_HINTS)


def _strip_thinking(text: str) -> str:
    if "</think>" not in text.lower():
        return text
    if "<think>" in text.lower():
        return _THINK_BLOCK_RE.sub("", text).strip()
    return _THINK_OPEN_RE.sub("", text, count=1).strip()


class _ThinkStripper:
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
        out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        if self._in_think:
            return ""
        out, self._buf = self._buf, ""
        return out


class LLMError(RuntimeError):
    """Safe Echo-facing model error."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        status: int | None = None,
        resolved_model: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status = status
        self.resolved_model = resolved_model
        self.latency_ms = latency_ms


class OpenAICompatibleLLM:
    """LLMPort adapter; all model I/O is delegated to ModelGatewayClient."""

    def __init__(
        self,
        settings: Settings,
        *,
        governor: PrincipalGovernor | None = None,
        gateway_client: ModelGatewayClient | None = None,
    ) -> None:
        self._settings = settings
        self._governor = governor
        self._gateway, self._gateway_config = create_model_gateway_client(settings, gateway_client)
        self._request_scheduler = _ModelRequestScheduler()

    async def aclose(self) -> None:
        await self._gateway.aclose()

    def _pick(self, model: str | None) -> tuple[ModelGatewayClient, str]:
        canonical = self._settings.llm_main_model.strip()
        requested = (model or canonical).strip()
        if requested != canonical:
            raise LLMError(
                "model route is not available",
                category="model_route_rejected",
                resolved_model=canonical,
            )
        return self._gateway, canonical

    def _effective_timeout(self, timeout_s: float | None) -> float:
        return self._settings.model_gateway_timeout_s if timeout_s is None else timeout_s

    async def resolve_chat_capability(self, *, timeout_s: float | None = None) -> object:
        """读取同一凭证的当前 chat capability，供业务预算动态收紧。"""

        gateway, use_model = self._pick(None)
        return await resolve_chat_capability(
            gateway,
            requested_model=use_model or None,
            timeout_s=self._effective_timeout(timeout_s),
        )

    @staticmethod
    def _estimate_prompt_tokens(messages: list[ChatMessage]) -> int:
        return max(1, (sum(len(message.content) for message in messages) + 3) // 4)

    @staticmethod
    def _estimate_prompt_context_tokens(messages: list[ChatMessage]) -> int:
        # The live profile publishes a context limit, but not the tokenizer used
        # by the selected model.  UTF-8 bytes are a model-independent upper
        # bound for content tokens; reserve additional room for the provider's
        # chat template and per-message role/control tokens.  The former
        # chars/4 estimate severely under-counted CJK prompts and let a derived
        # output_limit consume the whole context, which the upstream rejected.
        content_and_roles = sum(
            len(message.content.encode("utf-8")) + len(message.role.encode("utf-8"))
            for message in messages
        )
        framing = _CHAT_FRAMING_TOKEN_RESERVE + len(messages) * _CHAT_MESSAGE_TOKEN_RESERVE
        return max(1, content_and_roles + framing)

    @staticmethod
    def _validate_max_tokens(
        messages: list[ChatMessage],
        max_tokens: int,
        *,
        context_limit: int,
    ) -> None:
        if max_tokens < 1 or OpenAICompatibleLLM._estimate_prompt_context_tokens(messages) + max_tokens > context_limit:
            raise LLMError(
                "model context limit exceeded",
                category="context_length_exceeded",
                status=400,
            )

    @staticmethod
    def _effective_max_tokens(messages: list[ChatMessage], requested: int, capability: object) -> int:
        context_limit = int(getattr(capability, "context_window_tokens"))
        capability_limit = int(getattr(capability, "max_output_tokens"))
        prompt_estimate = OpenAICompatibleLLM._estimate_prompt_context_tokens(messages)
        return min(requested, capability_limit, max(1, context_limit - prompt_estimate))

    async def _reserve_tokens(self, messages: list[ChatMessage], max_tokens: int) -> tuple[QuotaReservation | None, int]:
        prompt_tokens = self._estimate_prompt_tokens(messages)
        if self._governor is None:
            return None, prompt_tokens
        reservation = await self._governor.reserve_llm_tokens(current_principal(), prompt_tokens + max_tokens)
        return reservation, prompt_tokens

    @staticmethod
    async def _release_reservation(reservation: QuotaReservation | None) -> None:
        if reservation is not None:
            await reservation.release()

    @staticmethod
    async def _settle_stream_reservation(reservation: QuotaReservation | None, *, prompt_estimate: int, output_chars: int) -> None:
        if reservation is not None:
            await reservation.settle(prompt_estimate + max(1, (output_chars + 3) // 4))

    @staticmethod
    def _build_kwargs(model: str, messages: list[ChatMessage], max_tokens: int, temperature: float, stream: bool) -> dict[str, object]:
        return {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [message.model_dump() for message in messages],
            "stream": stream,
        }

    @staticmethod
    def _usage(result: GatewayResult[str]) -> LLMUsage:
        usage = result.metadata.usage or {}
        return LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            completion_tokens=int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        top_p: float | None = None,
        min_p: float | None = None,
        repetition_penalty: float | None = None,
        seed: int | None = None,
        timeout_s: float | None = None,
        priority: LLMPriority = "foreground",
    ) -> LLMResponse:
        gateway, use_model = self._pick(model)
        request_timeout = self._effective_timeout(timeout_s)
        lease = await self._request_scheduler.acquire(priority)
        effective_max = max_tokens
        reservation: QuotaReservation | None = None
        prompt_estimate = self._estimate_prompt_tokens(messages)
        started = time.monotonic()
        capability = None
        try:
            capability = await resolve_chat_capability(
                gateway,
                requested_model=use_model or None,
                timeout_s=request_timeout,
            )
            if effective_max is None:
                effective_max = capability.max_output_tokens
            effective_max = self._effective_max_tokens(messages, effective_max, capability)
            self._validate_max_tokens(
                messages,
                effective_max,
                context_limit=capability.context_window_tokens,
            )
            reservation, prompt_estimate = await self._reserve_tokens(messages, effective_max)
            options = {"temperature": temperature}
            options.update({
                name: value
                for name, value in {
                    "top_p": top_p,
                    "min_p": min_p,
                    "repetition_penalty": repetition_penalty,
                    "seed": seed,
                }.items()
                if value is not None
            })
            result = await gateway.chat(
                [message.model_dump() for message in messages],
                max_tokens=effective_max,
                options=capability.options(options),
                policy=capability.policy,
                timeout_s=request_timeout,
            )
            logger.info(
                "model gateway chat capability=chat model=%s endpoint=%s http_status=%s latency_ms=%s retry_count=%s request_id=%s",
                capability.model,
                capability.endpoint,
                result.metadata.http_status,
                round(result.metadata.latency_ms, 1),
                result.metadata.retry_count,
                result.metadata.request_id,
            )
            content = _strip_thinking(result.result)
            usage = self._usage(result)
            if reservation is not None:
                await reservation.settle(usage.total_tokens or prompt_estimate + max(1, (len(content) + 3) // 4))
            return LLMResponse(content=content, model=capability.model, finish_reason="stop" if content else None,
                               usage=usage, latency_ms=result.metadata.latency_ms or (time.monotonic() - started) * 1000,
                               http_status=result.metadata.http_status)
        except ModelGatewayError as error:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.warning(
                "model_gateway_chat_failure resolved_model=%s category=%s status=%s "
                "attempt=1 latency_ms=%.1f",
                capability.model if capability is not None else "unknown",
                error.category,
                error.status,
                elapsed_ms,
            )
            await self._release_reservation(reservation)
            raise LLMError(
                "model gateway request failed",
                category=error.category,
                status=error.status,
                resolved_model=capability.model if capability is not None else None,
                latency_ms=elapsed_ms,
            ) from None
        except Exception:
            await self._release_reservation(reservation)
            raise LLMError("model gateway request failed") from None
        finally:
            await lease.release()

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        top_p: float | None = None,
        min_p: float | None = None,
        repetition_penalty: float | None = None,
        seed: int | None = None,
        timeout_s: float | None = None,
        priority: LLMPriority = "foreground",
    ) -> AsyncIterator[str]:
        gateway, use_model = self._pick(model)
        request_timeout = self._effective_timeout(timeout_s)
        lease = await self._request_scheduler.acquire(priority)
        effective_max = max_tokens
        prompt_estimate = self._estimate_prompt_tokens(messages)
        reservation: QuotaReservation | None = None
        output_chars = 0
        try:
            capability = await resolve_chat_capability(
                gateway,
                requested_model=use_model or None,
                timeout_s=request_timeout,
            )
            if effective_max is None:
                effective_max = capability.max_output_tokens
            effective_max = self._effective_max_tokens(messages, effective_max, capability)
            self._validate_max_tokens(
                messages,
                effective_max,
                context_limit=capability.context_window_tokens,
            )
            reservation, prompt_estimate = await self._reserve_tokens(messages, effective_max)
            options = {"temperature": temperature}
            options.update({
                name: value
                for name, value in {
                    "top_p": top_p,
                    "min_p": min_p,
                    "repetition_penalty": repetition_penalty,
                    "seed": seed,
                }.items()
                if value is not None
            })
            stripper = _ThinkStripper()
            async for chunk in gateway.iter_chat(
                [message.model_dump() for message in messages],
                max_tokens=effective_max,
                options=capability.options(options),
                policy=capability.policy,
                timeout_s=request_timeout,
            ):
                text = stripper.feed(chunk)
                if text:
                    output_chars += len(text)
                    yield text
            tail = stripper.flush()
            if tail:
                output_chars += len(tail)
                yield tail
        except ModelGatewayError as error:
            await self._release_reservation(reservation)
            reservation = None
            raise LLMError(
                "model gateway request failed",
                category=error.category,
                status=error.status,
            ) from None
        except Exception:
            await self._release_reservation(reservation)
            reservation = None
            raise LLMError("model gateway request failed") from None
        finally:
            await self._settle_stream_reservation(reservation, prompt_estimate=prompt_estimate, output_chars=output_chars)
            await lease.release()


__all__ = ["LLMError", "OpenAICompatibleLLM", "_is_reasoning", "_strip_thinking", "_ThinkStripper"]
