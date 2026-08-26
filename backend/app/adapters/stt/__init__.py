"""EchoDesk 唯一 STT 装配：Model Gateway → Qwen3-ASR。"""

from __future__ import annotations

import asyncio

from app.adapters.stt.model_gateway import ModelGatewaySTT, STTError
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.adapters.llm.model_gateway_factory import resolve_model_gateway_credential
from app.config import Settings
from app.ports.stt import STTPort

_scheduler: ASRScheduler | None = None
_startup_probe_task: asyncio.Task[None] | None = None

# A short, deterministic non-silent PCM16 sample.  The probe only verifies that
# the configured scheduler/provider path can accept and complete one bounded
# request; its transcript is intentionally ignored.
_STARTUP_PROBE_AUDIO = b"\x01\x00" * 1600


def make_stt(settings: Settings) -> STTPort:
    """返回唯一的 Model Gateway STT adapter。"""

    return ModelGatewaySTT(settings)


def build_asr_scheduler(
    settings: Settings,
    *,
    telemetry: object | None = None,
) -> ASRScheduler:
    """Build the ASR-owned scheduler without changing legacy call sites."""

    bindings: dict[str, ASRProviderBinding] = {}
    if settings.asr_scheduler_enabled:
        bindings["model_gateway"] = _build_model_gateway_binding(settings)

    scheduler_config = ASRSchedulerConfig(
        enabled=settings.asr_scheduler_enabled,
        eligible_providers=("model_gateway",),
        max_concurrency=settings.asr_scheduler_max_concurrency,
        queue_size=settings.asr_scheduler_queue_size,
        job_deadline_s=settings.asr_job_deadline_s,
        max_attempts=settings.asr_max_attempts,
        circuit_failure_threshold=settings.asr_circuit_failure_threshold,
        circuit_cooldown_s=settings.asr_circuit_cooldown_s,
        scope_max_concurrency=settings.asr_scope_max_concurrency,
        scope_rate_limit_per_minute=settings.asr_scope_rate_limit_per_minute,
        readiness_stale_after_s=settings.asr_readiness_stale_after_s,
    )
    return ASRScheduler(bindings, scheduler_config, telemetry=telemetry)


def _build_model_gateway_binding(settings: Settings) -> ASRProviderBinding:
    auth_ready = True
    adapter: STTPort = ModelGatewaySTT(settings, timeout_s=settings.asr_job_deadline_s)
    try:
        resolve_model_gateway_credential(
            base_url=settings.model_gateway_base_url,
            service_name=settings.model_gateway_service_name,
            configured_key=settings.model_gateway_api_key,
            configured_file=settings.model_gateway_api_key_file,
        )
    except RuntimeError:
        auth_ready = False
    refresh = getattr(adapter, "refresh_capability", None)
    return ASRProviderBinding(
        name="model_gateway",
        adapter=adapter,
        weight=1.0,
        max_concurrency=settings.asr_model_gateway_concurrency,
        auth_ready=auth_ready,
        transport="sse_one_shot",
        refresh_readiness=refresh if callable(refresh) else None,
    )


def get_asr_scheduler(
    settings: Settings,
    *,
    telemetry: object | None = None,
) -> ASRScheduler:
    """Return one process-wide scheduler for global queue/quota semantics."""

    global _scheduler  # noqa: PLW0603
    if _scheduler is None:
        _scheduler = build_asr_scheduler(settings, telemetry=telemetry)
    elif telemetry is not None:
        _scheduler.set_telemetry(telemetry)
    return _scheduler


async def start_asr_scheduler(
    settings: Settings,
    *,
    telemetry: object | None = None,
) -> ASRScheduler:
    global _startup_probe_task  # noqa: PLW0603
    scheduler = get_asr_scheduler(settings, telemetry=telemetry)
    await scheduler.refresh_provider_readiness()
    await scheduler.start()
    readiness = scheduler.readiness()
    if (
        settings.asr_scheduler_enabled
        and (_startup_probe_task is None or _startup_probe_task.done())
    ):
        timeout_s = min(settings.asr_job_deadline_s, settings.asr_readiness_stale_after_s)
        _startup_probe_task = asyncio.create_task(
            _run_controlled_probe_loop(
                scheduler,
                timeout_s=timeout_s,
                interval_s=settings.asr_readiness_stale_after_s / 2,
            ),
            name="asr-controlled-probe-loop",
        )
        _startup_probe_task.add_done_callback(_consume_startup_probe_task)
    return scheduler


async def _run_controlled_probe_loop(
    scheduler: ASRScheduler,
    *,
    timeout_s: float,
    interval_s: float,
) -> None:
    while True:
        await scheduler.refresh_provider_readiness()
        await scheduler.start()
        if scheduler.readiness().eligible_provider_count == 0:
            await asyncio.sleep(interval_s)
            continue
        await _run_controlled_probe(scheduler, timeout_s=timeout_s)
        await asyncio.sleep(interval_s)


async def _run_controlled_probe(
    scheduler: ASRScheduler,
    *,
    timeout_s: float,
) -> None:
    try:
        await asyncio.wait_for(
            scheduler.transcribe(
                _STARTUP_PROBE_AUDIO,
                sample_rate=16_000,
                language="zh",
                capability="startup_readiness",
            ),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        scheduler.record_controlled_probe(False)
    else:
        scheduler.record_controlled_probe(True)


def _consume_startup_probe_task(task: asyncio.Task[None]) -> None:
    global _startup_probe_task  # noqa: PLW0603
    if not task.cancelled():
        task.exception()
    if _startup_probe_task is task:
        _startup_probe_task = None


async def stop_asr_scheduler(*, grace_period_s: float = 5.0) -> None:
    global _scheduler, _startup_probe_task  # noqa: PLW0603
    probe_task = _startup_probe_task
    _startup_probe_task = None
    if probe_task is not None and not probe_task.done():
        probe_task.cancel()
        await asyncio.gather(probe_task, return_exceptions=True)
    if _scheduler is None:
        return
    scheduler = _scheduler
    _scheduler = None
    await scheduler.close(grace_period_s=grace_period_s)


def reset_asr_scheduler_for_test() -> None:
    """Reset only the ASR-owned process-wide lifecycle registry."""

    global _scheduler, _startup_probe_task  # noqa: PLW0603
    if _startup_probe_task is not None and not _startup_probe_task.done():
        _startup_probe_task.cancel()
    _startup_probe_task = None
    _scheduler = None


__all__ = [
    "ModelGatewaySTT",
    "STTError",
    "build_asr_scheduler",
    "get_asr_scheduler",
    "make_stt",
    "reset_asr_scheduler_for_test",
    "start_asr_scheduler",
    "stop_asr_scheduler",
]
