"""TTS API：POST /tts/speak → 直接返回 PCM bytes（16kHz 16-bit mono）。

前端在用户开了 TTS 开关后调本接口：
- chat 答完 / @总结会议 完 → fetch /tts/speak → AudioContext 播放
- 后端不主动推 WS 音频，避免 base64 大 payload；只推 ``tts.suggested`` 文字事件

phase4-tts 2026-05-28 加固（M_tts_check）：
- 上游 heyi cold-start 会偶尔返回"全 0 PCM"——adapter 现在算 RMS，
  本路由识别为静音→ 502 ``tts_silent_output`` 让前端能 message.error
  而不是让用户"看到绿灯按了播放却没声音"。
- 新增 GET /tts/diag：跑一次固定文本的真实合成回环，30 秒 cache。
  StatusBar 上的「TTS」健康指示从此读这个，不再只看 TCP 通不通。
- 每次 /tts/speak 都打 INFO log（文本长度 / latency / pcm bytes / rms）——
  以前 backend log 完全看不到 TTS 调用，事后定位全靠猜。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.tts.qwen3_tts import (
    SILENCE_RMS_FLOOR,
    Qwen3TTS,
    SynthesisResult,
    TTSError,
    is_silent,
)
from app.api.deps import get_event_bus
from app.config import Settings, get_settings
from app.use_cases.speak import SpeakUseCase, TtsKind

logger = logging.getLogger("echodesk.tts")
router = APIRouter(prefix="/tts", tags=["tts"])

_tts_singleton: Qwen3TTS | None = None

# /tts/diag 结果缓存：30s TTL。StatusBar 大约 10s 拉一次，cache 让 heyi
# 不会被 N 个客户端的轮询打爆；但 cache 也不能太长，否则状态显示滞后于现实。
_DIAG_CACHE_TTL_S = 30.0
# 真实合成的 probe 文本——短（< 6 字）、合法中文，避免触发任何拒答策略。
_DIAG_PROBE_TEXT = "测试一下"
_diag_lock = asyncio.Lock()
# 类型放到下面定义，这里用 Any 占位避免 forward-ref 与 UP037 的死循环。
_diag_cache: tuple[float, Any] | None = None


def get_tts_singleton(
    settings: Settings = Depends(get_settings),
) -> Qwen3TTS:
    """faster-qwen3-tts adapter 单例（详见 docs/ARCH-AUDIT.md §3）。"""
    global _tts_singleton  # noqa: PLW0603
    if _tts_singleton is None:
        _tts_singleton = Qwen3TTS(settings)
    return _tts_singleton


def get_speak_use_case(
    tts: Qwen3TTS = Depends(get_tts_singleton),
    bus: InMemoryEventBus = Depends(get_event_bus),
) -> SpeakUseCase:
    return SpeakUseCase(tts=tts, event_bus=bus)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    voice: str | None = None
    kind: TtsKind = "chat"


@router.post(
    "/speak",
    responses={
        200: {
            "content": {"audio/pcm": {}},
            "description": "PCM 16kHz 16-bit mono",
        },
        502: {"description": "upstream tts unavailable or returned silent output"},
        503: {"description": "tts disabled in settings"},
    },
)
async def tts_speak(
    body: Annotated[SpeakRequest, Body(...)],
    settings: Annotated[Settings, Depends(get_settings)],
    tts: Annotated[Qwen3TTS, Depends(get_tts_singleton)],
) -> Response:
    """合成语音并返回 PCM bytes。失败一律走 502 让前端 message.error。"""
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="tts disabled in settings")
    t0 = time.monotonic()
    try:
        result: SynthesisResult = await tts.synthesize_detailed(body.text, voice=body.voice)
    except TTSError as e:
        elapsed = time.monotonic() - t0
        logger.warning(
            "tts.speak fail: text_len=%d voice=%s elapsed=%.2fs err=%s",
            len(body.text),
            body.voice or tts.default_voice,
            elapsed,
            e,
        )
        raise HTTPException(status_code=502, detail=f"tts_upstream_error: {e}") from e
    if not result.pcm:
        raise HTTPException(status_code=400, detail="empty text")
    if is_silent(result):
        # cold-start / heyi 偶发：上游返回了字节但 RMS=0；如果默默把这串"假
        # 装合成成功"的静音 PCM 传回前端，UI 不会报错，只是没声音 —— 这正是
        # 用户口中的"TTS 完全失效"。这里诚实告诉前端：合成了，但是无声。
        logger.warning(
            "tts.speak silent output: text_len=%d voice=%s pcm_bytes=%d rms=%.1f latency=%.2fs",
            len(body.text),
            body.voice or tts.default_voice,
            len(result.pcm),
            result.rms,
            result.latency_s,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"tts_silent_output: upstream returned {len(result.pcm)} bytes "
                f"PCM but rms={result.rms:.1f} (< {SILENCE_RMS_FLOOR}); "
                "可能 heyi qwen3-tts 冷启动或被限流，请稍后重试"
            ),
        )
    logger.info(
        "tts.speak ok: text_len=%d voice=%s pcm_bytes=%d rms=%.1f peak=%d latency=%.2fs",
        len(body.text),
        body.voice or tts.default_voice,
        len(result.pcm),
        result.rms,
        result.max_abs,
        result.latency_s,
    )
    return Response(content=result.pcm, media_type="audio/pcm")


class SuggestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: TtsKind = "chat"
    meeting_id: str | None = None


@router.post("/suggest")
async def tts_suggest(
    body: Annotated[SuggestRequest, Body(...)],
    speak: Annotated[SpeakUseCase, Depends(get_speak_use_case)],
) -> dict[str, str]:
    """只推事件，不合成（让前端控制是否真的播）。多用于服务端主动触发场景。"""
    await speak.suggest(body.text, kind=body.kind, meeting_id=body.meeting_id)
    return {"status": "queued"}


# ── /tts/diag ────────────────────────────────────────────────────────
#
# 顶栏「TTS」健康指示从此读这个——只看 TCP 通不通是欺骗（heyi 服务"在线
# 但合成失败/静音"过去全部显示绿）。/tts/diag 跑一次真实合成回环再判定。


class DiagResult(BaseModel):
    """TTS 子系统健康快照。前端 StatusBar 直接渲染这套字段。"""

    ok: bool = Field(description="True 才表示合成 + 解码 + 非静音三件套通过")
    state: Literal["ok", "disabled", "upstream_error", "silent_output", "empty"] = Field(
        description="UI 取 state 走分支：ok 显绿、disabled 灰、其它红/橙"
    )
    detail: str | None = Field(default=None, description="人类可读说明；用于 Popover tooltip")
    latency_ms: float | None = None
    pcm_bytes: int | None = None
    rms: float | None = None
    peak: int | None = None
    voice: str | None = None
    base_url: str | None = None
    checked_at: float = Field(default_factory=time.time)


async def _run_diag_uncached(tts: Qwen3TTS, settings: Settings) -> DiagResult:
    """跑一次实际合成 probe。结果总是返回 DiagResult，绝不抛——失败编码进 state。"""
    if not settings.tts_enabled:
        return DiagResult(
            ok=False,
            state="disabled",
            detail="tts_enabled=false in settings",
            voice=tts.default_voice,
            base_url=tts.base_url,
        )
    try:
        result = await tts.synthesize_detailed(_DIAG_PROBE_TEXT)
    except TTSError as e:
        return DiagResult(
            ok=False,
            state="upstream_error",
            detail=str(e),
            voice=tts.default_voice,
            base_url=tts.base_url,
        )
    if not result.pcm:
        return DiagResult(
            ok=False,
            state="empty",
            detail="upstream returned empty audio",
            latency_ms=round(result.latency_s * 1000, 1),
            voice=tts.default_voice,
            base_url=tts.base_url,
        )
    if is_silent(result):
        return DiagResult(
            ok=False,
            state="silent_output",
            detail=(
                f"upstream returned {len(result.pcm)} bytes but rms={result.rms:.1f}"
                f" (< {SILENCE_RMS_FLOOR})"
            ),
            latency_ms=round(result.latency_s * 1000, 1),
            pcm_bytes=len(result.pcm),
            rms=round(result.rms, 1),
            peak=result.max_abs,
            voice=tts.default_voice,
            base_url=tts.base_url,
        )
    return DiagResult(
        ok=True,
        state="ok",
        detail=None,
        latency_ms=round(result.latency_s * 1000, 1),
        pcm_bytes=len(result.pcm),
        rms=round(result.rms, 1),
        peak=result.max_abs,
        voice=tts.default_voice,
        base_url=tts.base_url,
    )


@router.get("/diag", response_model=DiagResult)
async def tts_diag(
    settings: Annotated[Settings, Depends(get_settings)],
    tts: Annotated[Qwen3TTS, Depends(get_tts_singleton)],
    fresh: bool = False,
) -> DiagResult:
    """真实合成回环健康检查；30s cache 防止 UI 轮询打爆 heyi。

    ``?fresh=true`` 强制刷新（前端"立即重试"按钮用）。
    """
    global _diag_cache  # noqa: PLW0603
    now = time.time()
    if not fresh and _diag_cache is not None:
        cached_at, cached = _diag_cache
        if now - cached_at < _DIAG_CACHE_TTL_S:
            return cached
    async with _diag_lock:
        # 双重检查（lock 内）：可能等锁期间别人已经刷过 cache
        if not fresh and _diag_cache is not None:
            cached_at, cached = _diag_cache
            if now - cached_at < _DIAG_CACHE_TTL_S:
                return cached
        result = await _run_diag_uncached(tts, settings)
        _diag_cache = (time.time(), result)
        logger.info(
            "tts.diag: state=%s latency_ms=%s rms=%s peak=%s",
            result.state,
            result.latency_ms,
            result.rms,
            result.peak,
        )
        return result


def _reset_diag_cache_for_tests() -> None:
    """test-only hook：清空 diag cache，避免 test 间互相污染。"""
    global _diag_cache  # noqa: PLW0603
    _diag_cache = None


__all__ = [
    "DiagResult",
    "SpeakRequest",
    "SuggestRequest",
    "_reset_diag_cache_for_tests",
    "get_speak_use_case",
    "get_tts_singleton",
    "router",
    "tts_diag",
    "tts_speak",
    "tts_suggest",
]
