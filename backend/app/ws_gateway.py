"""
WebSocket gateway — manages device and desktop client connections.

Message protocol (JSON):

  Device → Server:
    {"type": "audio_chunk", "data": "<base64 PCM 16kHz 16-bit signed mono>"}
    {"type": "audio_end"}
    {"type": "ping"}

  Server → Device:
    {"type": "audio_chunk", "data": "<base64 PCM 16kHz 16-bit signed mono>"}  # TTS audio
    {"type": "audio_end"}    # 触发固件播放 + LED 复位到 IDLE
    {"type": "pong"}
    {"type": "proactive", "text": "...", "data": "<base64 PCM>"}
    {"type": "set_config", "key": "...", "value": "..."}

  Desktop → Server:
    {"type": "text", "text": "..."}
    {"type": "audio_chunk", "data": "<base64 PCM>"}   # 仅设备离线时生效
    {"type": "audio_end"}
    {"type": "set_tts", "enabled": true|false}

  Server → Desktop:
    {"type": "transcript",   "text": "...", "action": "activate|ambient|personal|ignore",
                             "source": "device|desktop", "speaker": "..."}
    {"type": "response_chunk", "text": "<single LLM token>"}  # per-token 流式，多条连续
    {"type": "response",     "text": "<full response>"}        # 流式结束信号（触发 FINISH_STREAMING）
    {"type": "emotion",      "pad": {"pleasure": f, "arousal": f, "dominance": f}}
    {"type": "memory_update", "nodes": [...]}
    {"type": "speaker",      "label": "...", "speaker_id": "...", "confidence": f}
    {"type": "bubble",       "text": "...", "role": "user|assistant"}
"""
from __future__ import annotations
import asyncio
import json
import base64
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from app.metrics import get_metrics


class ConnectionManager:
    def __init__(self) -> None:
        # device_id → WebSocket
        self._devices: dict[str, WebSocket] = {}
        # device_id → list of desktop WebSockets
        self._desktops: dict[str, list[WebSocket]] = {}
        # device_id → list of pending messages (audio chunks + audio_end) queued while offline
        self._pending: dict[str, list[dict]] = {}
        # 调试标志：设备连接后立即发测试音（用于硬件诊断）
        self.tone_on_connect: bool = False
        # 调试标志：忽略设备发来的音频，不触发STT管道
        self.mute_vad: bool = False

    # ── Connection lifecycle ──────────────────────────────────────

    async def connect_device(self, device_id: str, ws: WebSocket) -> None:
        await ws.accept()
        # Close stale connection if device reconnected before old WS loop exited
        old_ws = self._devices.get(device_id)
        if old_ws and old_ws is not ws:
            try:
                await old_ws.close()
            except Exception:
                pass
        self._devices[device_id] = ws
        logger.info(f"Device connected: {device_id}")
        get_metrics().record_deepgram_connect()

        # 调试模式：连接后立即发150ms 1000Hz测试音，用于硬件诊断
        if self.tone_on_connect:
            import math, struct
            sr, amp, dur = 16000, 30000, 0.15
            pcm = bytearray()
            for i in range(int(sr * dur)):
                pcm += struct.pack('<h', int(amp * math.sin(2 * math.pi * 1000 * i / sr)))
            tone_msg = {"type": "proactive", "text": "1kHz",
                        "data": base64.b64encode(bytes(pcm)).decode()}
            try:
                await ws.send_text(json.dumps(tone_msg, ensure_ascii=False))
                logger.info(f"[{device_id}] 已发送150ms 1kHz测试音（tone_on_connect模式）")
            except Exception as e:
                logger.warning(f"[{device_id}] 发送测试音失败: {e}")

        # Flush any pending messages (TTS responses generated while device was offline)
        pending = self._pending.pop(device_id, [])
        if pending:
            logger.info(f"Flushing {len(pending)} pending messages to [{device_id}]")
            for i, msg in enumerate(pending):
                try:
                    await ws.send_text(json.dumps(msg, ensure_ascii=False))
                    # Rate-limit: yield every 5 messages to avoid flooding device TCP buffer
                    if i % 5 == 4:
                        await asyncio.sleep(0.03)
                except Exception as e:
                    logger.warning(f"Failed to flush pending msg to [{device_id}]: {e}")
                    break

    def disconnect_device(self, device_id: str, ws: WebSocket = None) -> None:
        # If ws is given, only disconnect if it's still the current connection.
        # Guards against stale WS loops evicting a freshly reconnected device.
        if ws is not None and self._devices.get(device_id) is not ws:
            logger.debug(f"Stale WS loop exited for [{device_id}], ignoring")
            return
        self._devices.pop(device_id, None)
        logger.info(f"Device disconnected: {device_id}")
        get_metrics().record_deepgram_disconnect()

    async def connect_desktop(self, device_id: str, ws: WebSocket) -> None:
        await ws.accept()
        # 把新连接追加到列表；旧连接由客户端 cleanup 自己关闭
        # 不主动发 close(1001)，避免触发客户端的重连循环
        existing = self._desktops.get(device_id, [])
        self._desktops[device_id] = existing + [ws]
        logger.info(f"Desktop connected for device: {device_id}")

    def disconnect_desktop(self, device_id: str, ws: WebSocket) -> None:
        clients = self._desktops.get(device_id, [])
        if ws in clients:
            clients.remove(ws)
        logger.info(f"Desktop disconnected for device: {device_id}")

    # ── Send helpers ──────────────────────────────────────────────

    async def send_to_device(self, device_id: str, message: dict, queue_if_offline: bool = False) -> bool:
        ws = self._devices.get(device_id)
        if not ws:
            if queue_if_offline:
                self._pending.setdefault(device_id, []).append(message)
                logger.debug(f"Queued msg for offline device [{device_id}]: type={message.get('type')}")
            return False
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception as e:
            logger.warning(f"Failed to send to device {device_id}: {e}")
            self.disconnect_device(device_id, ws=ws)
            if queue_if_offline:
                self._pending.setdefault(device_id, []).append(message)
            return False

    async def send_audio_to_device(self, device_id: str, audio_bytes: bytes) -> bool:
        """Send TTS audio chunk — NOT queued if offline to prevent replay-flood on reconnect."""
        return await self.send_to_device(device_id, {
            "type": "audio_chunk",
            "data": base64.b64encode(audio_bytes).decode(),
        }, queue_if_offline=False)

    async def broadcast_to_desktops(self, device_id: str, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._desktops.get(device_id, []):
            try:
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_desktop(device_id, ws)

    # ── State queries ─────────────────────────────────────────────

    def is_device_online(self, device_id: str) -> bool:
        return device_id in self._devices

    def online_devices(self) -> list[str]:
        return list(self._devices.keys())


manager = ConnectionManager()


async def device_ws_endpoint(websocket: WebSocket, device_id: str) -> None:
    """FastAPI endpoint handler for device WebSocket connections."""
    from app.pipeline import Pipeline, is_in_tts_cooldown as _tts_cooldown
    from app.config import get_config as _get_config

    await manager.connect_device(device_id, websocket)

    # Legacy buffer (only used when STT_BACKEND != "deepgram")
    audio_buffer: list[bytes] = []
    audio_buf_start: float = 0.0
    MAX_AUDIO_BUF_BYTES = 80 * 1024
    MAX_AUDIO_BUF_SECS  = 5.0

    pipeline: Optional[Pipeline] = None

    try:
        pipeline = Pipeline(device_id=device_id, manager=manager)
        _use_deepgram = _get_config().STT_BACKEND == "deepgram"

        while True:
            try:
                packet = await asyncio.wait_for(websocket.receive(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.warning(f"Device [{device_id}] 60s idle, closing")
                break

            ptype = packet.get("type")
            if ptype == "websocket.disconnect":
                raise WebSocketDisconnect(packet.get("code", 1000))

            raw = packet.get("text") or (packet.get("bytes", b"") or b"").decode("utf-8", errors="replace")
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await manager.send_to_device(device_id, {"type": "pong"})

            elif msg_type == "audio_chunk":
                if manager.mute_vad:
                    audio_buffer.clear()
                    continue
                # TTS 冷却期：丢弃 TTS 余音，防止反馈环路
                if _tts_cooldown(device_id):
                    logger.debug(f"[{device_id}] audio_chunk dropped (TTS cooldown)")
                    continue
                chunk = base64.b64decode(msg["data"])

                if _use_deepgram:
                    # Deepgram mode: stream each chunk directly (no local buffering)
                    asyncio.create_task(pipeline.push_audio(chunk, source="device"))
                else:
                    # Legacy batch mode: accumulate then submit
                    if not audio_buffer:
                        audio_buf_start = asyncio.get_event_loop().time()
                    audio_buffer.append(chunk)
                    buf_size = sum(len(c) for c in audio_buffer)
                    buf_age  = asyncio.get_event_loop().time() - audio_buf_start
                    if buf_size >= MAX_AUDIO_BUF_BYTES or buf_age >= MAX_AUDIO_BUF_SECS:
                        audio_data = b"".join(audio_buffer)
                        audio_buffer.clear()
                        logger.info(f"Audio forced [{device_id}]: {len(audio_data)}B → STT")
                        asyncio.create_task(pipeline.handle_audio(audio_data))

            elif msg_type == "audio_end":
                if manager.mute_vad:
                    audio_buffer.clear()
                    continue
                # TTS 冷却期：丢弃这条 audio_end，防止空轮次触发 STT
                if _tts_cooldown(device_id):
                    audio_buffer.clear()
                    logger.debug(f"[{device_id}] audio_end dropped (TTS cooldown)")
                    continue
                if not _use_deepgram:
                    audio_data = b"".join(audio_buffer)
                    audio_buffer.clear()
                    if audio_data:
                        logger.info(f"Audio end [{device_id}]: {len(audio_data)}B → STT")
                        asyncio.create_task(pipeline.handle_audio(audio_data))

            elif msg_type == "text":
                asyncio.create_task(pipeline.handle_text(msg.get("text", "")))

    except WebSocketDisconnect:
        logger.info(f"Device WS closed normally [{device_id}]")
        manager.disconnect_device(device_id, ws=websocket)
    except Exception as e:
        logger.exception(f"Device WS error [{device_id}]: {e}")
        manager.disconnect_device(device_id, ws=websocket)
    finally:
        if pipeline is not None and pipeline.session.turns:
            try:
                await pipeline.session.save()
            except Exception as e:
                logger.warning(f"Device session save failed [{device_id}]: {e}")
        # Close Deepgram connection on device disconnect
        if _get_config().STT_BACKEND == "deepgram":
            try:
                from app.asr.deepgram_client import close_client
                await close_client(device_id, "device")
            except Exception:
                pass


async def desktop_ws_endpoint(websocket: WebSocket, device_id: str) -> None:
    """FastAPI endpoint handler for desktop WebSocket connections.

    Desktop audio streams to Deepgram in the same way device audio does.
    If ESP32 is online, desktop audio is skipped to avoid double-recording.
    """
    from app.config import get_config as _get_config

    await manager.connect_desktop(device_id, websocket)

    # Legacy buffer (only used when STT_BACKEND != "deepgram")
    audio_buffer: list[bytes] = []
    audio_buf_start: float = 0.0
    MAX_AUDIO_BUF_BYTES = 160 * 1024
    MAX_AUDIO_BUF_SECS  = 8.0

    pipeline = None

    try:
        _use_deepgram = _get_config().STT_BACKEND == "deepgram"

        while True:
            try:
                packet = await asyncio.wait_for(websocket.receive(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.debug(f"Desktop [{device_id}] 60s idle keepalive")
                continue

            ptype = packet.get("type")
            if ptype == "websocket.disconnect":
                raise WebSocketDisconnect(packet.get("code", 1000))

            raw = packet.get("text") or (packet.get("bytes", b"") or b"").decode("utf-8", errors="replace")
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if pipeline is None:
                from app.pipeline import Pipeline
                pipeline = Pipeline(device_id=device_id, manager=manager)

            if msg_type == "text":
                asyncio.create_task(pipeline.handle_text(msg.get("text", "")))

            elif msg_type == "set_tts":
                from app.pipeline import set_tts_enabled
                set_tts_enabled(device_id, bool(msg.get("enabled", True)))

            elif msg_type == "audio_chunk":
                # 当前 device_id 的设备在线时跳过桌面录音，避免双路拾音
                # 注意：用 is_device_online(device_id) 而非 online_devices()，
                # 防止多设备并存时误丢弃其他 device_id 的桌面音频
                if manager.is_device_online(device_id):
                    audio_buffer.clear()
                    continue
                chunk = base64.b64decode(msg["data"])
                logger.debug(f"Desktop audio_chunk [{device_id}]: {len(chunk)}B")

                if _use_deepgram:
                    asyncio.create_task(pipeline.push_audio(chunk, source="desktop"))
                else:
                    if not audio_buffer:
                        audio_buf_start = asyncio.get_event_loop().time()
                    audio_buffer.append(chunk)
                    buf_size = sum(len(c) for c in audio_buffer)
                    buf_age  = asyncio.get_event_loop().time() - audio_buf_start
                    if buf_size >= MAX_AUDIO_BUF_BYTES or buf_age >= MAX_AUDIO_BUF_SECS:
                        audio_data = b"".join(audio_buffer)
                        audio_buffer.clear()
                        logger.info(f"Desktop audio forced [{device_id}]: {len(audio_data)}B → STT")
                        asyncio.create_task(pipeline.handle_audio(audio_data, source="desktop"))

            elif msg_type == "audio_end":
                if manager.is_device_online(device_id):
                    audio_buffer.clear()
                    continue
                if not _use_deepgram:
                    audio_data = b"".join(audio_buffer)
                    audio_buffer.clear()
                    if audio_data:
                        logger.info(f"Desktop audio end [{device_id}]: {len(audio_data)}B → STT")
                        asyncio.create_task(pipeline.handle_audio(audio_data, source="desktop"))

    except WebSocketDisconnect:
        manager.disconnect_desktop(device_id, websocket)
    except Exception as e:
        logger.exception(f"Desktop WS error [{device_id}]: {e}")
        manager.disconnect_desktop(device_id, websocket)
    finally:
        if pipeline is not None and pipeline.session.turns:
            try:
                await pipeline.session.save()
            except Exception as e:
                logger.warning(f"Desktop session save failed [{device_id}]: {e}")
        # Close Deepgram desktop connection
        if _get_config().STT_BACKEND == "deepgram":
            try:
                from app.asr.deepgram_client import close_client
                await close_client(device_id, "desktop")
            except Exception:
                pass
