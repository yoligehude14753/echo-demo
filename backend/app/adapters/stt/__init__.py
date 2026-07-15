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
    scheduler = get_asr_scheduler(settings, telemetry=telemetry)
    await scheduler.start()
    return scheduler


async def stop_asr_scheduler(*, grace_period_s: float = 5.0) -> None:
    global _scheduler  # noqa: PLW0603
    if _scheduler is None:
        return
    scheduler = _scheduler
    _scheduler = None
    await scheduler.close(grace_period_s=grace_period_s)


def reset_asr_scheduler_for_test() -> None:
    """Reset only the ASR-owned process-wide lifecycle registry."""

    global _scheduler  # noqa: PLW0603
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
