"""
可观测性指标模块：统计 Deepgram 连接数、LLM Token 消耗、端到端延迟。

所有计数器均为进程内内存计数（重启清零）。如需持久化，接入 Prometheus Push Gateway。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from app.stt import get_asr_router_snapshot


@dataclass
class _Metrics:
    # Deepgram 连接
    deepgram_connections_total: int = 0
    deepgram_connections_active: int = 0

    # LLM Token 消耗
    llm_prompt_tokens_total: int = 0
    llm_completion_tokens_total: int = 0
    llm_requests_total: int = 0
    llm_errors_total: int = 0

    # 端到端延迟（VAD end → transcript 到达桌面）单位：秒
    e2e_latency_samples: list[float] = field(default_factory=list)

    # Router 分类分布
    router_action_counts: dict[str, int] = field(
        default_factory=lambda: {"activate": 0, "personal": 0, "ambient": 0, "ignore": 0}
    )

    _lock: Lock = field(default_factory=Lock, repr=False)

    def record_deepgram_connect(self) -> None:
        with self._lock:
            self.deepgram_connections_total += 1
            self.deepgram_connections_active += 1

    def record_deepgram_disconnect(self) -> None:
        with self._lock:
            self.deepgram_connections_active = max(0, self.deepgram_connections_active - 1)

    def record_llm_usage(self, prompt_tokens: int, completion_tokens: int, error: bool = False) -> None:
        with self._lock:
            self.llm_requests_total += 1
            self.llm_prompt_tokens_total += prompt_tokens
            self.llm_completion_tokens_total += completion_tokens
            if error:
                self.llm_errors_total += 1

    def record_e2e_latency(self, seconds: float) -> None:
        with self._lock:
            self.e2e_latency_samples.append(seconds)
            # 只保留最近 1000 个样本
            if len(self.e2e_latency_samples) > 1000:
                self.e2e_latency_samples = self.e2e_latency_samples[-1000:]

    def record_router_action(self, action: str) -> None:
        with self._lock:
            if action in self.router_action_counts:
                self.router_action_counts[action] += 1

    def e2e_latency_p50(self) -> float | None:
        with self._lock:
            if not self.e2e_latency_samples:
                return None
            s = sorted(self.e2e_latency_samples)
            return s[len(s) // 2]

    def e2e_latency_p95(self) -> float | None:
        with self._lock:
            if not self.e2e_latency_samples:
                return None
            s = sorted(self.e2e_latency_samples)
            return s[int(len(s) * 0.95)]


_metrics = _Metrics()


def get_metrics() -> _Metrics:
    return _metrics


def get_metrics_summary() -> dict:
    m = _metrics
    return {
        "asr": get_asr_router_snapshot(),
        "deepgram": {
            "connections_total": m.deepgram_connections_total,
            "connections_active": m.deepgram_connections_active,
        },
        "llm": {
            "requests_total": m.llm_requests_total,
            "prompt_tokens_total": m.llm_prompt_tokens_total,
            "completion_tokens_total": m.llm_completion_tokens_total,
            "errors_total": m.llm_errors_total,
        },
        "e2e_latency_s": {
            "p50": m.e2e_latency_p50(),
            "p95": m.e2e_latency_p95(),
            "sample_count": len(m.e2e_latency_samples),
        },
        "router": {
            "action_counts": dict(m.router_action_counts),
        },
        "collected_at": time.time(),
    }
