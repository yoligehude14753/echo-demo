"""STT adapter 集合。

demo 部署：STT 走受控语音识别服务。

当前**唯一**支持的 STT backend = **FireRed**（`firered` @ :8090）——见
docs/ARCH-AUDIT.md §2。SenseVoice 历史上作为 fallback 存在，已在 PR
`echodesk-remove-sensevoice` 删除，原因：架构判断时多 backend 选项会让人误判
"换 backend 能修 X"——实际所有 speaker explosion / ambient 幻觉问题都是
ambient_capture + diarizer 链路问题，跟 STT 模型无关。

未来本地化（demo 阶段需要离线运行时）保持 Ports & Adapters 模式以便扩展。
"""

from __future__ import annotations

import asyncio

from app.adapters.stt.firered import FireRedSTT, STTError
from app.adapters.stt.local import LocalSTT
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.adapters.stt.stepfun import (
    StepFunSettings,
    StepFunSSEOneShotSTT,
    StepFunWebSocketStreamSTT,
)
from app.config import Settings
from app.ports.stt import STTPort

_scheduler: ASRScheduler | None = None
_startup_probe_task: asyncio.Task[None] | None = None

# A short, deterministic non-silent PCM16 sample.  The probe only verifies that
# the configured scheduler/provider path can accept and complete one bounded
# request; its transcript is intentionally ignored.
_STARTUP_PROBE_AUDIO = b"\x01\x00" * 1600


def make_stt(settings: Settings) -> STTPort:
    """目前固定返回 FireRedSTT；保留 settings.stt_backend 字段供未来扩展。

    向后兼容：旧 .env 里 `STT_BACKEND=sensevoice_gpu` 会被忽略（不再支持），
    回退到 firered 并打日志（在 adapter 实例化处不显式 warn，以免 boot 时刷屏）。
    """
    backend = (settings.stt_backend or "").lower().strip()
    if backend and backend != "firered":
        # 旧值如 sensevoice_gpu / sensevoice / deepgram / whisper 等 → 统一忽略，
        # 走 firered。不抛错以免老 .env 升级时 backend 启动失败。
        import logging

        logging.getLogger("echodesk.stt").warning(
            "stt_backend=%r 已不再支持，统一回退到 firered（PR remove-sensevoice）",
            settings.stt_backend,
        )
    return FireRedSTT(settings)


def build_asr_scheduler(
    settings: Settings,
    *,
    telemetry: object | None = None,
) -> ASRScheduler:
    """Build the ASR-owned scheduler without changing legacy call sites."""

    bindings: dict[str, ASRProviderBinding] = {}
    if settings.asr_scheduler_enabled:
        for name in settings.asr_eligible_providers:
            binding = _build_binding(name, settings)
            if binding is not None:
                bindings[name] = binding

    scheduler_config = ASRSchedulerConfig(
        enabled=settings.asr_scheduler_enabled,
        eligible_providers=tuple(settings.asr_eligible_providers),
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


def _build_binding(name: str, settings: Settings) -> ASRProviderBinding | None:
    weight = settings.asr_provider_weights.get(name)
    concurrency = settings.asr_provider_concurrency.get(name)
    if weight is None or concurrency is None:
        return None
    if name == "firered":
        adapter: STTPort = FireRedSTT(settings, timeout_s=settings.asr_job_deadline_s)
        transport = "sse_one_shot"
    elif name == "stepfun":
        if not settings.asr_stepfun_enabled:
            return None
        stepfun_settings = StepFunSettings(
            api_key=settings.asr_stepfun_api_key,
            sse_url=settings.asr_stepfun_sse_url,
            websocket_url=settings.asr_stepfun_ws_url,
            sse_model=settings.asr_stepfun_sse_model,
            websocket_model=settings.asr_stepfun_ws_model,
            timeout_s=settings.asr_job_deadline_s,
            idle_timeout_s=settings.asr_stepfun_ws_idle_timeout_s,
            max_duration_s=settings.asr_stepfun_ws_max_duration_s,
            max_sessions=settings.asr_stepfun_ws_max_sessions,
            send_queue_size=settings.asr_stepfun_ws_send_queue_size,
        )
        if settings.asr_stepfun_transport == "sse_one_shot":
            adapter = StepFunSSEOneShotSTT(stepfun_settings)
        else:
            adapter = StepFunWebSocketStreamSTT(stepfun_settings)
        transport = settings.asr_stepfun_transport
    elif name == "local":
        if not settings.asr_local_enabled:
            return None
        adapter = LocalSTT(
            model_path=settings.asr_local_model_path,
            device=settings.asr_local_device,
            compute_type=settings.asr_local_compute_type,
            worker_count=settings.asr_local_worker_count,
        )
        transport = "local_worker"
    else:
        return None
    return ASRProviderBinding(
        name=name,
        adapter=adapter,
        weight=weight,
        max_concurrency=concurrency,
        transport=transport,  # type: ignore[arg-type]
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
    await scheduler.start()
    readiness = scheduler.readiness()
    if (
        settings.asr_scheduler_enabled
        and readiness.scheduler_accepting
        and readiness.eligible_provider_count > 0
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
    "FireRedSTT",
    "STTError",
    "build_asr_scheduler",
    "get_asr_scheduler",
    "make_stt",
    "reset_asr_scheduler_for_test",
    "start_asr_scheduler",
    "stop_asr_scheduler",
]
