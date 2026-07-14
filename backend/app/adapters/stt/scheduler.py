"""Bounded ASR scheduler and provider failover state machine.

The scheduler is specific to the existing ``STTPort`` boundary. It does not
share queue, cursor, retry, or backpressure state with the sync outbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.adapters.stt.contracts import (
    ASRProviderReadiness as _ASRProviderReadiness,
)
from app.adapters.stt.contracts import (
    ASRReadinessSnapshot,
    ASRRequestContext,
)
from app.adapters.stt.errors import (
    ASRAudioRejected,
    ASRDeadlineExceeded,
    ASRError,
    ASRIdempotencyConflict,
    ASRNoEligibleProvider,
    ASRProviderPermanentError,
    ASRProviderProtocolError,
    ASRProviderTransientError,
    ASRQueueFull,
    ASRRateLimited,
    ASRSchedulerDisabled,
    ASRSchedulerShutdown,
)
from app.ports.stt import STTPort
from app.schemas.meeting import TranscriptSegment

CircuitState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True, slots=True)
class ASRSchedulerConfig:
    """Validated runtime limits for one scheduler instance."""

    enabled: bool = False
    eligible_providers: tuple[str, ...] = ()
    max_concurrency: int = 1
    queue_size: int = 0
    job_deadline_s: float = 30.0
    max_attempts: int = 1
    circuit_failure_threshold: int = 3
    circuit_cooldown_s: float = 15.0
    scope_max_concurrency: int = 2
    scope_rate_limit_per_minute: int = 60
    readiness_stale_after_s: float = 30.0
    silence_threshold: int = 0
    min_audio_bytes: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "eligible_providers", tuple(self.eligible_providers))
        self._validate_limits()
        self._validate_provider_names()

    def _validate_limits(self) -> None:
        checks = (
            (self.max_concurrency < 1, "max_concurrency must be >= 1"),
            (self.queue_size < 0, "queue_size must be >= 0"),
            (self.job_deadline_s <= 0, "job_deadline_s must be > 0"),
            (self.max_attempts < 1, "max_attempts must be >= 1"),
            (self.circuit_failure_threshold < 1, "circuit_failure_threshold must be >= 1"),
            (self.circuit_cooldown_s <= 0, "circuit_cooldown_s must be > 0"),
            (self.scope_max_concurrency < 1, "scope_max_concurrency must be >= 1"),
            (self.scope_rate_limit_per_minute < 0, "scope_rate_limit_per_minute must be >= 0"),
            (self.readiness_stale_after_s <= 0, "readiness_stale_after_s must be > 0"),
            (
                self.silence_threshold < 0 or self.silence_threshold > 32_767,
                "silence_threshold must be in the PCM16 range",
            ),
            (self.min_audio_bytes < 2, "min_audio_bytes must be >= 2"),
        )
        for invalid, message in checks:
            if invalid:
                raise ValueError(message)

    def _validate_provider_names(self) -> None:
        if len(set(self.eligible_providers)) != len(self.eligible_providers):
            raise ValueError("eligible_providers must not contain duplicates")
        if any(not name.strip() for name in self.eligible_providers):
            raise ValueError("eligible_providers must contain non-empty names")


@dataclass(frozen=True, slots=True)
class ASRProviderBinding:
    """ASR-specific adapter binding and per-provider concurrency policy."""

    name: str
    adapter: STTPort
    weight: float = 1.0
    max_concurrency: int = 1
    enabled: bool = True
    auth_ready: bool = True
    transport: Literal["sse_one_shot", "websocket_stream", "local_worker"] = "sse_one_shot"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider name must not be empty")
        if self.weight <= 0:
            raise ValueError("provider weight must be > 0")
        if self.max_concurrency < 1:
            raise ValueError("provider max_concurrency must be >= 1")


@dataclass(slots=True)
class _ProviderState:
    circuit_state: CircuitState = "closed"
    consecutive_failures: int = 0
    open_until: float | None = None
    half_open_probe_reserved: bool = False
    in_flight: int = 0
    waiting: int = 0
    successes: int = 0
    failures: int = 0


@dataclass(slots=True)
class _ScopeState:
    in_flight: int = 0
    request_times: deque[float] = field(default_factory=deque)


@dataclass(slots=True)
class _QueuedJob:
    audio_bytes: bytes
    sample_rate: int
    language: str
    context: ASRRequestContext
    deadline_at: float
    future: asyncio.Future[list[TranscriptSegment]]
    scope_key: tuple[str, str, str]
    accepted_at: float
    cancelled: bool = False
    provider_task: asyncio.Task[list[TranscriptSegment]] | None = None
    provider_name: str | None = None
    last_provider_name: str | None = None
    queue_wait_ms: int = 0


@dataclass(frozen=True, slots=True)
class _IdempotencyHandle:
    future: asyncio.Future[list[TranscriptSegment]]


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    fingerprint: str
    expires_at: float
    future: asyncio.Future[list[TranscriptSegment]]


class ASRScheduler:
    """A bounded, cancellable scheduler over concrete ``STTPort`` adapters."""

    def __init__(
        self,
        providers: Mapping[str, ASRProviderBinding],
        config: ASRSchedulerConfig,
        *,
        clock: Any = time.monotonic,
        telemetry: object | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._config = config
        self._clock = clock
        self._telemetry = telemetry
        self._states = {name: _ProviderState() for name in self._providers}
        self._semaphores = {
            name: asyncio.Semaphore(binding.max_concurrency)
            for name, binding in self._providers.items()
        }
        # A queue size of zero is represented by the explicit accepted-count
        # guard. The internal queue still needs a positive maxsize for asyncio.
        self._queue: asyncio.Queue[_QueuedJob] = asyncio.Queue(maxsize=max(1, config.queue_size))
        self._workers: list[asyncio.Task[None]] = []
        self._scope_states: dict[tuple[str, str, str], _ScopeState] = {}
        self._idempotency: dict[tuple[tuple[str, str, str], str], _IdempotencyRecord] = {}
        self._accepted_count = 0
        self._active_jobs = 0
        self._state_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._last_controlled_probe_at: datetime | None = None
        self._last_controlled_probe_ok: bool | None = None
        self._metrics: dict[str, int] = {
            "accepted_total": 0,
            "completed_total": 0,
            "failed_total": 0,
            "rejected_total": 0,
            "idempotency_hits_total": 0,
            "idempotency_conflict_total": 0,
            "provider_failover_total": 0,
            "deadline_total": 0,
            "cancelled_total": 0,
        }

    def set_telemetry(self, telemetry: object | None) -> None:
        """Attach the process telemetry hook without changing queue state."""

        self._telemetry = telemetry

    async def start(self) -> None:
        """Start only scheduler workers for an enabled, configured candidate set."""

        async with self._state_lock:
            if self._started or self._closing:
                return
            self._started = True
            if not self._config.enabled or not self._has_configured_candidate():
                return
            self._workers = [
                asyncio.create_task(self._worker_loop(index), name=f"asr-scheduler-{index}")
                for index in range(self._config.max_concurrency)
            ]

    async def close(self, *, grace_period_s: float = 5.0) -> None:
        """Stop admission, drain accepted work within a bounded grace period."""

        if grace_period_s <= 0:
            raise ValueError("grace_period_s must be > 0")
        async with self._state_lock:
            if self._closing:
                return
            self._closing = True
            workers = list(self._workers)

        if workers:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=grace_period_s)
            except TimeoutError:
                await self._fail_queued_jobs(ASRSchedulerShutdown())
            finally:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        for binding in self._providers.values():
            close = getattr(binding.adapter, "aclose", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pragma: no cover - defensive shutdown boundary
                self._metrics["failed_total"] += 1

        async with self._state_lock:
            self._workers = []
            self._started = False

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
        context: ASRRequestContext | None = None,
        options: Mapping[str, object] | None = None,
        capability: str | None = None,
    ) -> list[TranscriptSegment]:
        """Admit and await one ASR job without blocking the event loop."""

        if not self._audio_is_admissible(audio_bytes):
            self._metrics["rejected_total"] += 1
            error = ASRAudioRejected()
            self._schedule_telemetry(
                context=context,
                audio_bytes=audio_bytes,
                provider=None,
                success=False,
                error=error,
                latency_ms=0,
                queue_wait_ms=0,
            )
            raise error

        request_context = context or ASRRequestContext(request_id=f"asr-{uuid.uuid4().hex}")
        await self.start()
        try:
            job, owns_job = await self._admit(
                audio_bytes,
                sample_rate=sample_rate,
                language=language,
                context=request_context,
                options=options,
                capability=capability,
            )
        except ASRError as error:
            self._schedule_telemetry(
                context=request_context,
                audio_bytes=audio_bytes,
                provider=None,
                success=False,
                error=error,
                latency_ms=0,
                queue_wait_ms=0,
            )
            raise
        try:
            return await asyncio.shield(job.future)
        except asyncio.CancelledError:
            if owns_job and isinstance(job, _QueuedJob):
                job.cancelled = True
                if job.provider_task is not None and not job.provider_task.done():
                    job.provider_task.cancel()
                if not job.future.done():
                    job.future.cancel()
                self._metrics["cancelled_total"] += 1
            raise

    async def _admit(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        language: str,
        context: ASRRequestContext,
        options: Mapping[str, object] | None,
        capability: str | None,
    ) -> tuple[_QueuedJob | _IdempotencyHandle, bool]:
        now = self._clock()
        deadline_s = min(
            context.deadline_s or self._config.job_deadline_s, self._config.job_deadline_s
        )
        deadline_at = now + deadline_s
        scope_key = context.scope_key
        idempotency_key = context.idempotency_key
        fingerprint = self._request_fingerprint(
            audio_bytes,
            sample_rate=sample_rate,
            language=language,
            context=context,
            options=options,
            capability=capability,
        )
        async with self._state_lock:
            self._prune_idempotency(now)
            if self._closing:
                self._metrics["rejected_total"] += 1
                raise ASRSchedulerShutdown(retry_after_s=1.0)
            if not self._config.enabled:
                self._metrics["rejected_total"] += 1
                raise ASRSchedulerDisabled()
            reused = self._reuse_idempotent_job(
                scope_key,
                idempotency_key,
                fingerprint,
            )
            if reused is not None:
                return reused

            retry_after = self._no_eligible_retry_after(now)
            if not self._has_eligible_candidate(now):
                self._metrics["rejected_total"] += 1
                raise ASRNoEligibleProvider(retry_after_s=retry_after)

            scope = self._scope_states.setdefault(scope_key, _ScopeState())
            self._prune_scope(scope, now)
            if scope.in_flight >= self._config.scope_max_concurrency:
                self._metrics["rejected_total"] += 1
                raise ASRRateLimited(retry_after_s=1.0)
            if (
                self._config.scope_rate_limit_per_minute > 0
                and len(scope.request_times) >= self._config.scope_rate_limit_per_minute
            ):
                retry_after_s = max(1.0, 60.0 - (now - scope.request_times[0]))
                self._metrics["rejected_total"] += 1
                raise ASRRateLimited(retry_after_s=retry_after_s)

            capacity = self._config.max_concurrency + self._config.queue_size
            if self._accepted_count >= capacity:
                self._metrics["rejected_total"] += 1
                raise ASRQueueFull(retry_after_s=1.0)

            loop = asyncio.get_running_loop()
            job = _QueuedJob(
                audio_bytes=audio_bytes,
                sample_rate=sample_rate,
                language=language,
                context=context,
                deadline_at=deadline_at,
                future=loop.create_future(),
                scope_key=scope_key,
                accepted_at=now,
            )
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull as error:
                self._metrics["rejected_total"] += 1
                raise ASRQueueFull(retry_after_s=1.0) from error
            self._accepted_count += 1
            scope.in_flight += 1
            scope.request_times.append(now)
            if idempotency_key is not None:
                self._idempotency[(scope_key, idempotency_key)] = _IdempotencyRecord(
                    fingerprint=fingerprint,
                    expires_at=now + max(60.0, self._config.job_deadline_s * 4),
                    future=job.future,
                )
            self._metrics["accepted_total"] += 1
            return job, True

    async def _worker_loop(self, _index: int) -> None:
        while True:
            job = await self._queue.get()
            job.queue_wait_ms = max(0, round((self._clock() - job.accepted_at) * 1000))
            async with self._state_lock:
                self._active_jobs += 1
            started_at = self._clock()
            outcome_error: BaseException | None = None
            outcome_success = False
            try:
                if job.cancelled or job.future.cancelled():
                    continue
                result = await self._execute(job)
                outcome_success = True
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.set_exception(ASRSchedulerShutdown())
                raise
            except ASRError as error:
                outcome_error = error
                if isinstance(error, ASRDeadlineExceeded):
                    self._metrics["deadline_total"] += 1
                if not job.future.done():
                    job.future.set_exception(error)
            except Exception as error:  # pragma: no cover - defensive adapter boundary
                outcome_error = error
                if not job.future.done():
                    job.future.set_exception(ASRProviderPermanentError())
                del error
            finally:
                if not job.cancelled:
                    self._schedule_telemetry(
                        context=job.context,
                        audio_bytes=job.audio_bytes,
                        provider=job.last_provider_name,
                        success=outcome_success,
                        error=outcome_error,
                        latency_ms=max(0, round((self._clock() - started_at) * 1000)),
                        queue_wait_ms=job.queue_wait_ms,
                    )
                await self._finish_job(job)
                self._queue.task_done()

    async def _execute(self, job: _QueuedJob) -> list[TranscriptSegment]:
        tried: set[str] = set()
        last_error: ASRError | None = None
        for attempt in range(self._config.max_attempts):
            remaining = job.deadline_at - self._clock()
            if remaining <= 0:
                raise ASRDeadlineExceeded()

            selected = await self._select_provider(tried)
            if selected is None:
                if last_error is not None:
                    raise ASRNoEligibleProvider(retry_after_s=1.0) from last_error
                raise ASRNoEligibleProvider(
                    retry_after_s=self._no_eligible_retry_after(self._clock())
                )
            name, binding = selected
            tried.add(name)
            try:
                result = await self._call_provider(binding, job, remaining)
                await self._record_provider_success(name)
                return result
            except asyncio.CancelledError:
                raise
            except ASRDeadlineExceeded:
                await self._record_provider_failure(name)
                raise
            except Exception as error:
                safe_error = self._as_safe_provider_error(error)
                await self._record_provider_failure(name)
                last_error = safe_error
                if not safe_error.retryable:
                    raise safe_error from error
                if attempt + 1 < self._config.max_attempts:
                    self._metrics["provider_failover_total"] += 1
                    if len(tried) >= len(self._configured_names()):
                        tried.clear()
                    continue
                raise safe_error from error
        raise last_error or ASRProviderPermanentError()

    def _schedule_telemetry(
        self,
        *,
        context: ASRRequestContext | None,
        audio_bytes: bytes,
        provider: str | None,
        success: bool,
        error: BaseException | None,
        latency_ms: int,
        queue_wait_ms: int,
    ) -> None:
        if context is None or self._telemetry is None:
            return
        task = asyncio.create_task(
            self._emit_telemetry(
                context=context,
                audio_bytes=audio_bytes,
                provider=provider,
                success=success,
                error=error,
                latency_ms=latency_ms,
                queue_wait_ms=queue_wait_ms,
            ),
            name=f"asr-telemetry:{context.request_id}",
        )
        task.add_done_callback(self._consume_telemetry_task)

    async def _emit_telemetry(
        self,
        *,
        context: ASRRequestContext,
        audio_bytes: bytes,
        provider: str | None,
        success: bool,
        error: BaseException | None,
        latency_ms: int,
        queue_wait_ms: int,
    ) -> None:
        record_asr = getattr(self._telemetry, "record_asr", None)
        if not callable(record_asr):
            return
        try:
            await record_asr(
                context=context,
                provider=provider,
                success=success,
                error=error,
                latency_ms=latency_ms,
                queue_wait_ms=queue_wait_ms,
                audio_duration_ms=round(len(audio_bytes) / 2 / 16_000 * 1000),
            )
        except Exception:
            # Test doubles and shutdown races must be fail-soft as well.
            return

    @staticmethod
    def _consume_telemetry_task(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _call_provider(
        self,
        binding: ASRProviderBinding,
        job: _QueuedJob,
        timeout_s: float,
    ) -> list[TranscriptSegment]:
        state = self._states[binding.name]
        semaphore = self._semaphores[binding.name]
        async with self._state_lock:
            state.waiting += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=max(0.001, timeout_s))
                acquired = True
            except TimeoutError as error:
                raise ASRDeadlineExceeded() from error
            async with self._state_lock:
                state.waiting -= 1
                state.in_flight += 1
            job.provider_name = binding.name
            job.last_provider_name = binding.name
            provider_task = asyncio.create_task(
                binding.adapter.transcribe(
                    job.audio_bytes,
                    sample_rate=job.sample_rate,
                    language=job.language,
                )
            )
            job.provider_task = provider_task
            try:
                result = await asyncio.wait_for(provider_task, timeout=max(0.001, timeout_s))
            except TimeoutError as error:
                raise ASRDeadlineExceeded() from error
            if not isinstance(result, list) or any(
                not isinstance(segment, TranscriptSegment) for segment in result
            ):
                raise ASRProviderProtocolError()
            return result
        finally:
            job.provider_task = None
            job.provider_name = None
            if acquired:
                async with self._state_lock:
                    state.in_flight -= 1
                semaphore.release()
            else:
                async with self._state_lock:
                    state.waiting = max(0, state.waiting - 1)

    async def _select_provider(
        self,
        tried: set[str],
    ) -> tuple[str, ASRProviderBinding] | None:
        async with self._state_lock:
            now = self._clock()
            candidates: list[tuple[float, int, str, ASRProviderBinding]] = []
            for order, name in enumerate(self._configured_names()):
                if name in tried:
                    continue
                binding = self._providers.get(name)
                state = self._states.get(name)
                if (
                    binding is None
                    or state is None
                    or not binding.enabled
                    or not binding.auth_ready
                ):
                    continue
                if state.circuit_state == "open":
                    if state.open_until is None or now < state.open_until:
                        continue
                    state.circuit_state = "half_open"
                    state.half_open_probe_reserved = False
                if state.circuit_state == "half_open":
                    if state.half_open_probe_reserved:
                        continue
                    state.half_open_probe_reserved = True
                score = (state.in_flight + state.waiting + 1) / binding.weight
                candidates.append((score, order, name, binding))

            if not candidates:
                return None
            available = [
                item
                for item in candidates
                if self._states[item[2]].in_flight + self._states[item[2]].waiting
                < item[3].max_concurrency
            ]
            selected = min(available or candidates, key=lambda item: (item[0], item[1]))
            return selected[2], selected[3]

    async def _record_provider_success(self, name: str) -> None:
        async with self._state_lock:
            state = self._states[name]
            state.consecutive_failures = 0
            state.circuit_state = "closed"
            state.open_until = None
            state.half_open_probe_reserved = False
            state.successes += 1

    async def _record_provider_failure(self, name: str) -> None:
        async with self._state_lock:
            state = self._states[name]
            state.failures += 1
            state.consecutive_failures += 1
            if (
                state.circuit_state == "half_open"
                or state.consecutive_failures >= self._config.circuit_failure_threshold
            ):
                state.circuit_state = "open"
                state.open_until = self._clock() + self._config.circuit_cooldown_s
                state.half_open_probe_reserved = False

    async def _finish_job(self, job: _QueuedJob) -> None:
        async with self._state_lock:
            self._accepted_count = max(0, self._accepted_count - 1)
            self._active_jobs = max(0, self._active_jobs - 1)
            scope = self._scope_states.get(job.scope_key)
            if scope is not None:
                scope.in_flight = max(0, scope.in_flight - 1)
            if job.future.cancelled():
                return
            if job.future.exception() is None:
                self._metrics["completed_total"] += 1
            else:
                self._metrics["failed_total"] += 1

    async def _fail_queued_jobs(self, error: ASRError) -> None:
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if not job.future.done():
                    job.future.set_exception(error)
                await self._finish_job(job)
            finally:
                self._queue.task_done()

    def record_controlled_probe(self, ok: bool, *, checked_at: datetime | None = None) -> None:
        """Record an externally controlled probe result without doing work here."""

        self._last_controlled_probe_at = checked_at or datetime.now(UTC)
        self._last_controlled_probe_ok = ok

    def readiness(self) -> ASRReadinessSnapshot:
        now = self._clock()
        provider_readiness: dict[str, _ASRProviderReadiness] = {}
        eligible = 0
        for name in self._configured_names():
            binding = self._providers.get(name)
            state = self._states.get(name)
            if binding is None or state is None:
                continue
            is_eligible = self._is_state_eligible(name, now)
            if is_eligible:
                eligible += 1
            provider_readiness[name] = _ASRProviderReadiness(
                circuit_state=state.circuit_state,
                in_flight=state.in_flight,
                max_concurrency=binding.max_concurrency,
                eligible=is_eligible,
                auth_ready=binding.auth_ready,
            )
        available = max(
            0, self._config.max_concurrency + self._config.queue_size - self._accepted_count
        )
        return ASRReadinessSnapshot(
            scheduler_accepting=(
                self._config.enabled and not self._closing and self._has_configured_candidate()
            ),
            queue_capacity=self._config.max_concurrency + self._config.queue_size,
            queue_available=available,
            eligible_provider_count=eligible,
            active_jobs=self._active_jobs,
            worker_count=len(self._workers),
            checked_at=self._last_controlled_probe_at,
            probe_ok=self._last_controlled_probe_ok,
            providers=provider_readiness,
            reason_code="asr_probe_not_run"
            if self._last_controlled_probe_at is None
            else "asr_probe_recorded",
            retry_after_s=self._no_eligible_retry_after(now),
        )

    def metrics_snapshot(self) -> dict[str, int | float]:
        """Return aggregate metrics only; transcript text never enters this map."""

        snapshot: dict[str, int | float] = dict(self._metrics)
        snapshot.update(
            {
                "queue_size": self._queue.qsize(),
                "queue_capacity": self._config.max_concurrency + self._config.queue_size,
                "active_jobs": self._active_jobs,
                "eligible_provider_count": self.readiness().eligible_provider_count,
            }
        )
        return snapshot

    def capability_transports(self) -> dict[str, str]:
        """Return internal configured capability transport labels for diagnostics/tests."""

        return {name: binding.transport for name, binding in self._providers.items()}

    @staticmethod
    def latency_report(samples_ms: Sequence[float]) -> dict[str, float | int]:
        """Return raw-count percentile evidence for deterministic load tests."""

        if not samples_ms:
            raise ValueError("samples_ms must not be empty")
        ordered = sorted(float(sample) for sample in samples_ms)

        def percentile(p: float) -> float:
            index = min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1)
            return round(ordered[index], 3)

        return {
            "sample_count": len(ordered),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
        }

    def _configured_names(self) -> tuple[str, ...]:
        return self._config.eligible_providers

    def _has_configured_candidate(self) -> bool:
        return any(
            name in self._providers
            and self._providers[name].enabled
            and self._providers[name].auth_ready
            for name in self._configured_names()
        )

    def _has_eligible_candidate(self, now: float) -> bool:
        return any(self._is_state_eligible(name, now) for name in self._configured_names())

    def _is_state_eligible(self, name: str, now: float) -> bool:
        binding = self._providers.get(name)
        state = self._states.get(name)
        if binding is None or state is None or not binding.enabled or not binding.auth_ready:
            return False
        if state.circuit_state == "open" and (state.open_until is None or now < state.open_until):
            return False
        return not (state.circuit_state == "half_open" and state.half_open_probe_reserved)

    def _no_eligible_retry_after(self, now: float) -> float | None:
        cooldowns = [
            max(0.1, state.open_until - now)
            for name, state in self._states.items()
            if name in self._configured_names()
            and state.circuit_state == "open"
            and state.open_until is not None
            and state.open_until > now
        ]
        return min(cooldowns) if cooldowns else None

    def _audio_is_admissible(self, audio_bytes: bytes) -> bool:
        if len(audio_bytes) < self._config.min_audio_bytes:
            return False
        if self._config.silence_threshold == 0:
            return any(audio_bytes)
        if len(audio_bytes) % 2:
            return any(audio_bytes)
        threshold = self._config.silence_threshold
        return any(
            abs(int.from_bytes(audio_bytes[index : index + 2], "little", signed=True)) > threshold
            for index in range(0, len(audio_bytes), 2)
        )

    def _prune_scope(self, scope: _ScopeState, now: float) -> None:
        while scope.request_times and now - scope.request_times[0] >= 60.0:
            scope.request_times.popleft()

    def _prune_idempotency(self, now: float) -> None:
        expired = [key for key, record in self._idempotency.items() if record.expires_at <= now]
        for key in expired:
            del self._idempotency[key]

    def _reuse_idempotent_job(
        self,
        scope_key: tuple[str, str, str],
        idempotency_key: str | None,
        fingerprint: str,
    ) -> tuple[_QueuedJob | _IdempotencyHandle, bool] | None:
        if idempotency_key is None:
            return None
        record = self._idempotency.get((scope_key, idempotency_key))
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            self._metrics["idempotency_conflict_total"] += 1
            raise ASRIdempotencyConflict()
        self._metrics["idempotency_hits_total"] += 1
        return _IdempotencyHandle(record.future), False

    def _request_fingerprint(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        language: str,
        context: ASRRequestContext,
        options: Mapping[str, object] | None,
        capability: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
                "sample_rate": sample_rate,
                "language": language,
                "capability": capability or context.capability or "scheduler",
                "options": self._canonicalize_options(
                    options if options is not None else context.options
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _canonicalize_options(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): cls._canonicalize_options(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._canonicalize_options(item) for item in value]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise ValueError("ASR options must be JSON-compatible")

    @staticmethod
    def _as_safe_provider_error(error: Exception) -> ASRError:
        if isinstance(error, ASRError):
            return error
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return ASRProviderTransientError()
        return ASRProviderTransientError()


__all__ = ["ASRProviderBinding", "ASRScheduler", "ASRSchedulerConfig"]
