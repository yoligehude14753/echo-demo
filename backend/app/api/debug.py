"""调试 API：硬件诊断工具（需要 X-Debug-Token 保护，生产环境应关闭）。"""

from __future__ import annotations

import asyncio
import math
import struct

from fastapi import APIRouter, Depends, Header, HTTPException

from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings, get_settings
from app.schemas.llm import ChatMessage
from app.ws.manager import manager

router = APIRouter(prefix="/api/debug", tags=["debug"])


def _require_debug_token(
    x_debug_token: str = Header(default=""),
    cfg: Settings = Depends(get_settings),
) -> None:
    """当 DEBUG_TOKEN 非空时，校验请求头中的 token。"""
    if cfg.debug_token and x_debug_token != cfg.debug_token:
        raise HTTPException(status_code=403, detail="Debug token 无效")


@router.post("/tone_on_connect", dependencies=[Depends(_require_debug_token)])
async def set_tone_on_connect(enable: bool = True):
    """(v1 兼容占位，v2 中不再使用连接即测音。)"""
    return {"tone_on_connect": enable, "note": "v2 协议不再支持此功能"}


@router.post("/mute_vad", dependencies=[Depends(_require_debug_token)])
async def set_mute_vad(enable: bool = True):
    """(v1 兼容占位，v2 中 VAD 由服务端驱动。)"""
    return {"mute_vad": enable, "note": "v2 协议不再支持此功能"}


@router.get("/tone_long/{device_id}", dependencies=[Depends(_require_debug_token)])
async def tone_long(device_id: str, freq: int = 1000, duration: float = 5.0):
    """向设备发送流式长测试音（每块 1024B PCM）。适合硬件扬声器诊断。"""
    online = manager.online_devices()
    if device_id not in online:
        return {"error": f"设备 {device_id} 不在线", "online": online}

    sample_rate = 16000
    amplitude = 32000
    n_samples = int(sample_rate * duration)
    chunk_samples = 512
    total_chunks = 0
    total_bytes = 0

    for start in range(0, n_samples, chunk_samples):
        end = min(start + chunk_samples, n_samples)
        pcm = bytearray()
        for i in range(start, end):
            val = int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate))
            pcm += struct.pack('<h', val)

        ok = await manager.send_audio_to_device(device_id, bytes(pcm))
        if not ok:
            return {"error": "发送中断，设备已离线", "sent_chunks": total_chunks}
        total_chunks += 1
        total_bytes += len(pcm)
        # 控制发送速率：512 样本 = 32ms 音频，发送间隔 8ms
        await asyncio.sleep(0.008)

    await manager.send_to_device(device_id, {"type": "audio_end"})
    return {
        "sent": True,
        "device_id": device_id,
        "freq_hz": freq,
        "duration_s": duration,
        "total_chunks": total_chunks,
        "total_pcm_bytes": total_bytes,
    }


@router.get("/tone_test/{device_id}", dependencies=[Depends(_require_debug_token)])
async def tone_test(device_id: str, freq: int = 1000):
    """向设备发送短促蜂鸣测试音（150ms binary PCM v2 帧）。"""
    sample_rate = 16000
    amplitude = 30000
    duration = 0.15
    n_samples = int(sample_rate * duration)
    pcm = bytearray()
    for i in range(n_samples):
        val = int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate))
        pcm += struct.pack('<h', val)

    online = manager.online_devices()
    if device_id not in online:
        return {"error": f"设备 {device_id} 不在线", "online": online}

    ok = await manager.send_audio_to_device(device_id, bytes(pcm))
    return {
        "sent": ok,
        "device_id": device_id,
        "freq_hz": freq,
        "pcm_bytes": len(pcm),
    }


@router.get("/llm/test", dependencies=[Depends(_require_debug_token)])
async def llm_test(prompt: str = "你好，Echo！", cfg: Settings = Depends(get_settings)):
    adapter = OpenAICompatibleLLM(cfg)
    try:
        response = await adapter.chat([
            ChatMessage(role="system", content="你是 Echo，一个温柔的 AI 伴侣。"),
            ChatMessage(role="user", content=prompt),
        ], max_tokens=256)
    finally:
        await adapter.aclose()
    return {
        "model": response.model,
        "prompt": prompt,
        "response": response.content,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        },
    }


@router.get("/tts/test", dependencies=[Depends(_require_debug_token)])
async def tts_test(text: str = "你好，我是 Echo，扬声器测试正常。"):
    """TTS 诊断接口：合成音频并以 WAV 文件形式返回，可直接在浏览器播放。"""
    import io
    import time
    import wave

    from fastapi.responses import StreamingResponse

    from app.tts import EDGE_TTS_AVAILABLE, MINIAUDIO_AVAILABLE, synthesize_stream

    cfg = get_settings()
    t0 = time.monotonic()
    chunks: list[bytes] = []
    try:
        async for chunk in synthesize_stream(text):
            chunks.append(chunk)
    except Exception as e:
        from loguru import logger
        logger.exception(f"TTS test failed: {e}")
        return {
            "error": str(e),
            "provider": cfg.tts_provider,
            "miniaudio": MINIAUDIO_AVAILABLE,
            "edge_tts": EDGE_TTS_AVAILABLE,
        }

    pcm = b"".join(chunks)
    elapsed = time.monotonic() - t0

    if not pcm:
        return {
            "error": "TTS 返回空数据",
            "provider": cfg.tts_provider,
            "text": text,
            "elapsed_s": round(elapsed, 2),
            "hint": "请检查 TTS_PROVIDER 配置及 API Key",
        }

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="audio/wav",
        headers={
            "X-TTS-Provider": cfg.tts_provider,
            "X-PCM-Bytes": str(len(pcm)),
            "X-Duration-S": f"{len(pcm)/2/16000:.2f}",
            "X-Elapsed-S": f"{elapsed:.2f}",
        },
    )
