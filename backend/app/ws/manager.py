"""
ConnectionManager — 设备 WebSocket 连接注册表（v2）

职责：
- 管理 device_id → WebSocket 的映射
- binary / text 帧发送
- 离线时 pending 队列（仅用于控制消息，不缓存音频）
- stale connection guard（重连时关闭旧 WS）
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger

from app.ws.protocol import DeviceState, FrameType, encode_frame, encode_state_frame


class ConnectionManager:
    def __init__(self) -> None:
        self._devices: dict[str, WebSocket] = {}
        self._pending: dict[str, list[dict]] = defaultdict(list)

    # ── 设备连接管理 ──────────────────────────────────────────────────────────

    async def connect_device(self, device_id: str, ws: WebSocket) -> None:
        await ws.accept()
        # 先注册新 WS（旧 endpoint finally 块检查时会看到 current ≠ old，不会误调 close_session）
        # 不主动 close 旧 WS：active close 会导致新 WS 立即收到 disconnect 帧（Starlette 竞态）。
        # 旧 endpoint 会在设备切换连接后自然检测到断线并退出。
        self._devices[device_id] = ws
        pending = list(self._pending.pop(device_id, []))
        for msg in pending:
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                break
        logger.info(f"[Manager] device={device_id} 已连接，flush {len(pending)} 条 pending")

    def disconnect_device(self, device_id: str, ws: WebSocket) -> bool:
        """
        断开设备连接。若 ws 是当前注册的连接则移除并返回 True，
        若是旧连接（设备已重连）则不驱逐新 WS，返回 False。
        """
        current = self._devices.get(device_id)
        if current is ws:
            del self._devices[device_id]
            logger.info(f"[Manager] device={device_id} 已断开")
            return True
        return False

    def is_device_online(self, device_id: str) -> bool:
        return device_id in self._devices

    def online_devices(self) -> list[str]:
        return list(self._devices.keys())

    # ── 发送到设备（binary audio）────────────────────────────────────────────

    # ESP32-C3 ring buffer = 64KB ≈ 2s；需要限速防止溢出。
    # 每块 3200B = 100ms @ 16kHz 16-bit mono；以 85ms 间隔发送（略快于实时以防下溢）。
    _AUDIO_CHUNK_BYTES  = 3200   # 100ms
    _AUDIO_CHUNK_SLEEP  = 0.085  # 85ms pace — ~85% realtime, 留 15% 提前量

    async def send_audio_to_device(self, device_id: str, pcm: bytes) -> bool:
        """发送 TTS PCM 到设备，按 100ms 切块并限速，防止 C3 ring buffer 溢出。"""
        ws = self._devices.get(device_id)
        if ws is None:
            return False
        try:
            chunks = range(0, len(pcm), self._AUDIO_CHUNK_BYTES)
            for idx, i in enumerate(chunks):
                frame = encode_frame(FrameType.AUDIO, pcm[i:i + self._AUDIO_CHUNK_BYTES])
                await ws.send_bytes(frame)
                # 每发一块睡 85ms，让 ring buffer 有时间消费，避免溢出卡顿
                if idx < len(chunks) - 1:
                    await asyncio.sleep(self._AUDIO_CHUNK_SLEEP)
            return True
        except Exception as e:
            logger.warning(f"[Manager] send_audio 失败 device={device_id}: {e}")
            self.disconnect_device(device_id, ws)
            return False

    async def send_state_to_device(self, device_id: str, state: DeviceState) -> bool:
        """发送 STATE 帧驱动设备状态机（IDLE/LISTENING/THINKING/SPEAKING）。"""
        ws = self._devices.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(encode_state_frame(state))
            logger.debug(f"[Manager] STATE {state.name} → device={device_id}")
            return True
        except Exception as e:
            logger.warning(f"[Manager] send_state 失败 device={device_id}: {e}")
            self.disconnect_device(device_id, ws)
            return False

    async def send_frame_to_device(
        self, device_id: str, frame_type: FrameType, payload: bytes = b""
    ) -> bool:
        """发送任意 binary 帧（AUDIO_CANCEL、PING/PONG 等）到设备。"""
        ws = self._devices.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(encode_frame(frame_type, payload))
            return True
        except Exception as e:
            logger.warning(f"[Manager] send_frame 失败 device={device_id}: {e}")
            self.disconnect_device(device_id, ws)
            return False

    # ── 发送到设备（text JSON 控制消息）──────────────────────────────────────

    async def send_to_device(
        self,
        device_id: str,
        msg: dict[str, Any],
        queue_if_offline: bool = False,
    ) -> bool:
        """发送 JSON 控制消息（set_config / proactive 等）。"""
        ws = self._devices.get(device_id)
        if ws is None:
            if queue_if_offline:
                self._pending[device_id].append(msg)
            return False
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception as e:
            logger.warning(f"[Manager] send_to_device 失败 device={device_id}: {e}")
            self.disconnect_device(device_id, ws)
            return False


# 全局单例
manager = ConnectionManager()
