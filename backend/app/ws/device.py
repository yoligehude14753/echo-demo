"""
设备 WebSocket 端点（v2）— /ws/v2/device/{device_id}

协议（binary frame，见 ws/protocol.py）：
  上行（设备→后端）：
    0x01 AUDIO     — PCM 16kHz mono int16 LE，设备在 LISTENING 状态发送
    0x03 AUDIO_END — 已废弃，保留兼容接收
    0x10 PING      — 心跳

  下行（后端→设备）：
    0x01 AUDIO        — Doubao TTS PCM
    0x02 STATE        — 设备状态命令（IDLE/LISTENING/THINKING/SPEAKING）
    0x04 AUDIO_CANCEL — 打断
    0x11 PONG         — 心跳响应
"""
from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from app.s2s.session import close_session, get_or_create_session
from app.ws.manager import manager
from app.ws.protocol import DeviceState, FrameDecodeError, FrameType, decode_frame

# 上行音频帧计数器（用于诊断麦克风）
_audio_frame_counters: dict[str, int] = {}


async def device_ws_v2_endpoint(ws: WebSocket, device_id: str) -> None:
    """
    v2 设备 WebSocket 主处理函数，由 main.py 注册到 /ws/v2/device/{device_id}。

    设计原则：后端是"事件翻译层"，Doubao 是大脑，设备是哑终端。
    Doubao 事件 → 设备 STATE 命令，STATE 驱动设备麦克风开关和 LED/OLED 状态。
    """
    await manager.connect_device(device_id, ws)
    logger.info(f"[WS-v2] device={device_id} 已连接")

    # ── 回调：Doubao TTS PCM → 转发给设备 ──────────────────────────────────
    async def on_audio(pcm: bytes) -> None:
        await manager.send_audio_to_device(device_id, pcm)

    async def on_state(state: DeviceState) -> None:
        await manager.send_state_to_device(device_id, state)

    async def on_error(msg: str) -> None:
        await manager.send_to_device(device_id, {"type": "error", "message": msg})
        logger.error(f"[WS-v2] device={device_id} S2S 错误: {msg}")

    # ── 后台建立 Doubao 会话（不阻塞 WS 主循环）──────────────────────────────
    session_error: list[Exception] = []
    session_ready = asyncio.Event()

    async def _init_session() -> None:
        try:
            await get_or_create_session(device_id, on_audio, on_state, on_error)
            session_ready.set()
            logger.info(f"[WS-v2] device={device_id} Doubao 会话就绪")
        except Exception as e:
            session_error.append(e)
            session_ready.set()
            logger.error(f"[WS-v2] device={device_id} 会话建立失败: {e}")

    asyncio.create_task(_init_session(), name=f"init-session-{device_id}")

    # ── 主消息循环 ────────────────────────────────────────────────────────────
    try:
        while True:
            data = await ws.receive()

            if "bytes" in data and data["bytes"] is not None:
                raw: bytes = data["bytes"]
                try:
                    ftype, payload = decode_frame(raw)
                except FrameDecodeError as e:
                    logger.warning(f"[WS-v2] device={device_id} 帧解码失败: {e}")
                    continue

                if ftype == FrameType.AUDIO:
                    # 会话就绪前的早期帧直接丢弃（启动噪音，不发给 Doubao）
                    if session_ready.is_set() and not session_error:
                        session = _get_session(device_id)
                        if session:
                            await session.send_audio(payload)
                            # 每 200 帧（~200ms）打一次 DEBUG 确认上行音频正常
                            _audio_frame_counters[device_id] = (
                                _audio_frame_counters.get(device_id, 0) + 1
                            )
                            if _audio_frame_counters[device_id] % 200 == 0:
                                logger.debug(
                                    f"[WS-v2] device={device_id} 上行音频 "
                                    f"{_audio_frame_counters[device_id]} 帧 "
                                    f"({len(payload)}B/帧)"
                                )

                elif ftype == FrameType.AUDIO_END:
                    # 已废弃：设备不再需要发送 AUDIO_END（Doubao server_vad 控制）
                    logger.debug(f"[WS-v2] device={device_id} 收到废弃的 AUDIO_END（忽略）")

                elif ftype == FrameType.AUDIO_CANCEL:
                    session = _get_session(device_id)
                    if session:
                        await session.cancel()
                    logger.info(f"[WS-v2] device={device_id} barge-in")

                elif ftype == FrameType.PING:
                    await manager.send_frame_to_device(device_id, FrameType.PONG)

            elif "text" in data and data["text"]:
                try:
                    msg = json.loads(data["text"])
                except json.JSONDecodeError:
                    logger.warning(f"[WS-v2] device={device_id} 非法 JSON")
                    continue
                await _handle_text_msg(device_id, msg)

    except WebSocketDisconnect:
        logger.info(f"[WS-v2] device={device_id} 断开连接")
    except Exception as e:
        if "disconnect" in str(e).lower() or "receive" in str(e).lower():
            logger.info(f"[WS-v2] device={device_id} 连接断开: {e}")
        else:
            logger.error(f"[WS-v2] device={device_id} 循环异常: {e}")
    finally:
        was_current = manager.disconnect_device(device_id, ws)
        if was_current:
            await close_session(device_id)


def _get_session(device_id: str):
    """获取已建立的 S2S 会话（不创建新会话）。"""
    from app.s2s.session import _sessions
    return _sessions.get(device_id)


async def _handle_text_msg(device_id: str, msg: dict) -> None:
    """处理设备发来的 JSON 控制消息。"""
    mtype = msg.get("type")
    if mtype == "ping":
        await manager.send_to_device(device_id, {"type": "pong"})
    elif mtype == "text":
        session = _get_session(device_id)
        if session:
            await session.send_text(msg.get("text", ""))
    else:
        logger.debug(f"[WS-v2] device={device_id} 未处理消息 type={mtype}")
