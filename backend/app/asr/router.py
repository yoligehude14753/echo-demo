"""ASR provider 编排：平滑加权轮询、故障转移与熔断。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

ProviderCall = Callable[[bytes, str], Awaitable[str]]


def _csv(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """解析逗号分隔配置，并保持首次出现顺序。"""
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    result: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _weights(value: str | Mapping[str, int], names: tuple[str, ...]) -> dict[str, int]:
    """解析 ``provider=weight`` 配置；缺失或非法值使用 1。"""
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {}
        for item in value.split(","):
            if "=" not in item:
                continue
            name, weight = item.split("=", 1)
            try:
                raw[name.strip().lower()] = int(weight.strip())
            except ValueError:
                continue

    return {
        name: max(1, int(raw.get(name, 1)))
        for name in names
    }


@dataclass(frozen=True)
class RouterSettings:
    """路由器运行参数；所有值由 Config 注入。"""

    backend: str
    fallback_backends: tuple[str, ...]
    balanced_backends: tuple[str, ...]
    weights: dict[str, int]
    failure_threshold: int
    cooldown_s: float

    @classmethod
    def from_values(
        cls,
        *,
        backend: str,
        fallback_backends: str,
        balanced_backends: str,
        weights: str | Mapping[str, int],
        failure_threshold: int,
        cooldown_s: float,
    ) -> "RouterSettings":
        balanced = _csv(balanced_backends)
        return cls(
            backend=backend.strip().lower(),
            fallback_backends=_csv(fallback_backends),
            balanced_backends=balanced,
            weights=_weights(weights, balanced),
            failure_threshold=max(1, int(failure_threshold)),
            cooldown_s=max(0.0, float(cooldown_s)),
        )


@dataclass
class ProviderState:
    """单个 provider 的可观测健康状态。"""

    consecutive_failures: int = 0
    total_requests: int = 0
    successes: int = 0
    failures: int = 0
    empty_results: int = 0
    in_flight: int = 0
    circuit_open_until: float = 0.0
    half_open_probe: bool = False
    last_error: str = ""


class ASRProviderRouter:
    """把统一的 ``transcribe`` 请求路由到可替换的 provider。"""

    def __init__(
        self,
        providers: Mapping[str, ProviderCall],
        settings: RouterSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers = {name.strip().lower(): call for name, call in providers.items()}
        self._settings = settings
        self._clock = clock
        self._states = {name: ProviderState() for name in self._providers}
        self._smooth_current = {name: 0 for name in settings.balanced_backends}
        self._lock = asyncio.Lock()

    async def transcribe(self, audio_bytes: bytes, prompt: str = "") -> str:
        """按路由策略尝试 provider，返回首个非空文本。"""
        for name in await self._candidate_order():
            if not await self._reserve(name):
                continue

            state = self._states[name]
            state.total_requests += 1
            try:
                text = await self._providers[name](audio_bytes, prompt)
            except Exception as exc:
                await self._record_failure(name, exc)
                logger.warning(
                    "[ASR][router] provider={} failed type={} error={} "
                    "consecutive_failures={}",
                    name,
                    type(exc).__name__,
                    str(exc)[:200],
                    state.consecutive_failures,
                )
                continue
            finally:
                await self._release(name)

            if text and text.strip():
                await self._record_success(name)
                return text.strip()

            await self._record_empty(name)
            logger.info("[ASR][router] provider={} returned empty text", name)

        logger.error("[ASR][router] no provider produced a transcript")
        return ""

    async def _candidate_order(self) -> tuple[str, ...]:
        async with self._lock:
            if self._settings.backend == "balanced":
                configured = self._settings.balanced_backends
                first = self._smooth_pick(configured)
                ordered = (first,) + tuple(name for name in configured if name != first)
            else:
                ordered = (self._settings.backend,) + self._settings.fallback_backends

            result: list[str] = []
            for name in ordered:
                if name in self._providers and name not in result:
                    result.append(name)
            return tuple(result)

    def _smooth_pick(self, names: tuple[str, ...]) -> str:
        """平滑加权轮询，避免随机数造成短窗口内的尖峰。"""
        if not names:
            return ""
        total = sum(self._settings.weights.get(name, 1) for name in names)
        selected = names[0]
        for name in names:
            weight = self._settings.weights.get(name, 1)
            self._smooth_current[name] = self._smooth_current.get(name, 0) + weight
            if self._smooth_current[name] > self._smooth_current[selected]:
                selected = name
        self._smooth_current[selected] -= total
        return selected

    async def _reserve(self, name: str) -> bool:
        async with self._lock:
            state = self._states[name]
            now = self._clock()
            if state.circuit_open_until > now:
                return False
            if state.circuit_open_until and state.half_open_probe:
                return False
            if state.circuit_open_until:
                state.half_open_probe = True
            state.in_flight += 1
            return True

    async def _release(self, name: str) -> None:
        async with self._lock:
            self._states[name].in_flight = max(0, self._states[name].in_flight - 1)

    async def _record_success(self, name: str) -> None:
        async with self._lock:
            state = self._states[name]
            state.successes += 1
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0
            state.half_open_probe = False
            state.last_error = ""

    async def _record_failure(self, name: str, error: Exception) -> None:
        async with self._lock:
            state = self._states[name]
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error = type(error).__name__
            if state.consecutive_failures >= self._settings.failure_threshold:
                state.circuit_open_until = self._clock() + self._settings.cooldown_s
                state.half_open_probe = False

    async def _record_empty(self, name: str) -> None:
        async with self._lock:
            state = self._states[name]
            state.empty_results += 1
            # provider 已经正常返回；空文本不应继承旧的连续异常或占用半开探测位。
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0
            state.half_open_probe = False

    def snapshot(self) -> dict[str, Any]:
        """返回不包含凭证的 provider 状态，用于 health/metrics。"""
        now = self._clock()
        providers: dict[str, dict[str, Any]] = {}
        for name, state in self._states.items():
            if state.circuit_open_until > now:
                circuit = "open"
            elif state.circuit_open_until:
                circuit = "half_open"
            else:
                circuit = "closed"
            providers[name] = {
                "circuit": circuit,
                "consecutive_failures": state.consecutive_failures,
                "total_requests": state.total_requests,
                "successes": state.successes,
                "failures": state.failures,
                "empty_results": state.empty_results,
                "in_flight": state.in_flight,
                "last_error": state.last_error,
            }
        return {
            "backend": self._settings.backend,
            "fallback_backends": list(self._settings.fallback_backends),
            "balanced_backends": list(self._settings.balanced_backends),
            "weights": dict(self._settings.weights),
            "providers": providers,
        }
