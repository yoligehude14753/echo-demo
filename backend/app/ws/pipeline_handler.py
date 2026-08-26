"""
设备 WebSocket 端点（v2 Pipeline 模式）—— Xiaozhi MVP 架构

流程（参照小智 esp32-server 设计）：
  LISTENING  → 积累设备上传的 AUDIO 帧
  沉默 800ms → STATE=THINKING → stt.transcribe()
  ASR 完成   → stream_complete() → 累积 LLM 响应文本
  LLM 完成   → synthesize()    → 得到 16kHz mono PCM
  STATE=SPEAKING → 分块发送 PCM → STATE=LISTENING

替代 Doubao Realtime S2S（doubao.py / session.py），核心优势：
  · TTS 输出为标准 16kHz 16-bit mono PCM，无立体声混淆问题
  · ASR / LLM / TTS 三个环节可独立替换
  · 不依赖 Doubao Realtime WebSocket 协议细节
"""
from __future__ import annotations

import asyncio
import math
import struct

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from app.llm import stream_complete
from app.session.manager import SessionManager
from app.session.prompt_builder import build_system_prompt
from app.stt import transcribe
from app.tts import synthesize_stream
from app.ws.manager import manager
from app.ws.protocol import DeviceState, FrameDecodeError, FrameType, decode_frame, encode_frame  # noqa: F401

# ── 参数 ──────────────────────────────────────────────────────────────────────
_SILENCE_S        = 0.80   # 连续静音多久后触发 ASR（秒）
_MIN_AUDIO_BYTES  = 6400   # 最少累积字节（200ms @ 16kHz 16-bit）才提交 ASR
_RMS_THRESHOLD    = 2000   # int16 RMS 阈值：需要足够高才能过滤扬声器回声（直接说话通常 3000+）
_POST_TTS_COOLDOWN = 3.0   # TTS 播完后等待（秒），让回声彻底消散再开麦，防反馈死循环

# ── 会话历史（device_id → SessionManager） ────────────────────────────────────
_session_managers: dict[str, SessionManager] = {}


def _get_session(device_id: str) -> SessionManager:
    if device_id not in _session_managers:
        _session_managers[device_id] = SessionManager(device_id)
    return _session_managers[device_id]


def _rms(pcm: bytes) -> float:
    """计算 int16 LE PCM 的 RMS。"""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    sq_sum = sum(s * s for s in struct.unpack_from(f"<{n}h", pcm))
    return math.sqrt(sq_sum / n)


async def device_ws_pipeline_endpoint(ws: WebSocket, device_id: str) -> None:
    """
    Xiaozhi MVP 三段式管线 WebSocket 处理函数。
    由 main.py 注册到 /ws/v2/device/{device_id}（替代旧 device_ws_v2_endpoint）。
    """
    await manager.connect_device(device_id, ws)
    logger.info(f"[Pipeline] device={device_id} 已连接")

    # 立即进入 LISTENING 状态
    await manager.send_state_to_device(device_id, DeviceState.LISTENING)

    audio_buf  = bytearray()   # 本轮积累的 PCM
    processing = False         # 正在 ASR/LLM/TTS 期间，忽略新音频
    _silence_handle: asyncio.TimerHandle | None = None
    _loop = asyncio.get_event_loop()

    # ── 核心处理：ASR → LLM → TTS ───────────────────────────────────────────
    async def _process() -> None:
        nonlocal processing, audio_buf, _silence_handle

        accumulated = bytes(audio_buf)
        audio_buf.clear()
        processing = True

        # 1. ASR
        await manager.send_state_to_device(device_id, DeviceState.THINKING)
        logger.info(f"[Pipeline] device={device_id} ASR 开始 ({len(accumulated)}B)")
        try:
            text = await transcribe(accumulated)
        except Exception as e:
            logger.error(f"[Pipeline] ASR 失败: {e}")
            text = ""

        if not text.strip():
            logger.info(f"[Pipeline] device={device_id} ASR 空结果，回到 LISTENING")
            await manager.send_state_to_device(device_id, DeviceState.LISTENING)
            processing = False
            return

        logger.info(f"[Pipeline] device={device_id} ASR: {text!r}")

        # 2. LLM（build_messages 会把 add_turn 后的历史打包进去）
        sess = _get_session(device_id)
        sess.add_turn("user", text)
        system_prompt = build_system_prompt()
        messages = sess.build_messages(system_prompt)
        response_parts: list[str] = []
        try:
            async for chunk in stream_complete(messages):
                response_parts.append(chunk)
        except Exception as e:
            logger.error(f"[Pipeline] LLM 失败: {e}")
            response_parts = ["抱歉，我遇到了一点问题。"]

        response_text = "".join(response_parts)
        sess.add_turn("assistant", response_text)
        logger.info(f"[Pipeline] device={device_id} LLM: {response_text[:60]!r}…")

        # 3. TTS：先完整积累所有 PCM，再匀速发送（避免 SSE 分块不均导致 ring buffer 欠载爆音）
        await manager.send_state_to_device(device_id, DeviceState.SPEAKING)
        pcm_all = bytearray()
        try:
            async for pcm_chunk in synthesize_stream(response_text):
                if pcm_chunk:
                    pcm_all.extend(pcm_chunk)
        except Exception as e:
            logger.error(f"[Pipeline] TTS 失败: {e}")

        total_bytes = len(pcm_all)
        logger.info(f"[Pipeline] device={device_id} TTS 共 {total_bytes}B ({total_bytes/2/16000:.1f}s)，开始限速发送")

        # 匀速发送：100ms 块 + 85ms 间隔，保持 ring buffer 稳定充裕
        if total_bytes > 0:
            await manager.send_audio_to_device(device_id, bytes(pcm_all))

        # 4. 发送完毕后等待：ring buffer 余量（~800ms）+ 回声消散冷却
        #    共等待 _POST_TTS_COOLDOWN 秒，确保麦克风开启时扬声器已完全停止
        await asyncio.sleep(_POST_TTS_COOLDOWN)
        audio_buf.clear()  # 清除冷却期内可能积累的回声帧

        await manager.send_state_to_device(device_id, DeviceState.LISTENING)
        processing = False

    def _schedule_silence() -> None:
        """延迟 _SILENCE_S 后触发 _process（每次收到音频帧时重置）。"""
        nonlocal _silence_handle
        if _silence_handle is not None:
            _silence_handle.cancel()
        _silence_handle = _loop.call_later(
            _SILENCE_S,
            lambda: asyncio.ensure_future(_maybe_process()),
        )

    async def _maybe_process() -> None:
        nonlocal _silence_handle
        _silence_handle = None
        if processing or len(audio_buf) < _MIN_AUDIO_BYTES:
            return
        await _process()

    # ── 主接收循环 ────────────────────────────────────────────────────────────
    try:
        while True:
            data = await ws.receive()

            if data.get("type") == "websocket.disconnect":
                break
            if "bytes" not in data or data["bytes"] is None:
                continue

            raw: bytes = data["bytes"]
            try:
                ftype, payload = decode_frame(raw)
            except FrameDecodeError as e:
                logger.warning(f"[Pipeline] device={device_id} 帧解码失败: {e}")
                continue

            if ftype == FrameType.PING:
                await ws.send_bytes(encode_frame(FrameType.PONG, b""))

            elif ftype == FrameType.AUDIO:
                if processing:
                    continue  # 处理中忽略新音频（无 barge-in，MVP 简化）

                # RMS 过低的帧视为静音，不计入有效语音
                if _rms(payload) < _RMS_THRESHOLD:
                    _schedule_silence()  # 依然重置计时器（安静后触发）
                    continue

                audio_buf.extend(payload)
                _schedule_silence()

    except WebSocketDisconnect:
        logger.info(f"[Pipeline] device={device_id} 断开连接")
    except Exception as e:
        logger.error(f"[Pipeline] device={device_id} 异常: {e}", exc_info=True)
    finally:
        if _silence_handle is not None:
            _silence_handle.cancel()
        manager.disconnect_device(device_id, ws)
