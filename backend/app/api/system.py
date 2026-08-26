"""系统级 API：健康检查、音频配置、指标。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Config, get_config
from app.metrics import get_metrics_summary
from app.stt import get_asr_router_snapshot

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(cfg: Config = Depends(get_config)):
    return {
        "status": "ok",
        "model": cfg.LLM_MODEL,
        "tts": cfg.TTS_PROVIDER,
        "stt": cfg.STT_BACKEND,
        "version": "0.4.0",
    }


@router.get("/api/config/audio")
async def get_audio_config(cfg: Config = Depends(get_config)):
    """返回当前 ASR 配置、StepFun 开关和 provider 健康状态。"""
    return {
        "stt_backend":               cfg.STT_BACKEND,
        "stt_fallback_backends":     cfg.STT_FALLBACK_BACKENDS,
        "stt_load_balance_providers": cfg.STT_LOAD_BALANCE_PROVIDERS,
        "stt_load_balance_weights":  cfg.STT_LOAD_BALANCE_WEIGHTS,
        "stepfun": {
            "configured": bool(cfg.stepfun_api_key),
            "model": cfg.STEPFUN_ASR_MODEL,
            "base_url": cfg.STEPFUN_BASE_URL,
        },
        "deepgram_language":         cfg.DEEPGRAM_LANGUAGE,
        "deepgram_endpointing_ms":   cfg.DEEPGRAM_ENDPOINTING_MS,
        "deepgram_diarize":          cfg.DEEPGRAM_DIARIZE,
        "diarizer_enabled":          cfg.DIARIZER_ENABLED,
        "diarizer_match_threshold":  cfg.DIARIZER_MATCH_THRESHOLD,
        "router_min_text_len":       cfg.ROUTER_MIN_TEXT_LEN,
        "router_model":              cfg.ROUTER_MODEL,
        "provider_health":            get_asr_router_snapshot(),
    }


@router.get("/api/firmware/version")
async def firmware_version():
    """OTA version check endpoint for ESP32 firmware. Returns current backend version."""
    return {"version": "0.4.0", "update_available": False}


@router.get("/metrics")
async def metrics(cfg: Config = Depends(get_config)):
    """Prometheus 兼容文本格式指标（可被 Grafana Agent 抓取）。"""
    if not cfg.METRICS_ENABLED:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="metrics disabled")
    return get_metrics_summary()
