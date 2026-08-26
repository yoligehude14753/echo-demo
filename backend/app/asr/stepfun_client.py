"""StepFun StepAudio 2.5 ASR Stream WebSocket 适配器。"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import websockets
from loguru import logger

from app.asr.base import TranscriptSegment


class StepFunASRError(RuntimeError):
    """StepFun 连接、协议或服务端错误。"""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True)
class StepFunASRSettings:
    """由 Config 注入的 StepFun 参数，不在 adapter 内读取环境变量。"""

    api_key: str
    ws_url: str
    model: str
    language: str
    silence_duration_ms: int
    vad_threshold: float
    enable_itn: bool
    full_rerun_on_commit: bool
    connect_timeout_s: float
    response_timeout_s: float


SegmentCallback = Callable[[TranscriptSegment], Awaitable[None]]


def build_session_update(
    settings: StepFunASRSettings,
    *,
    prompt: str = "",
    server_vad: bool = False,
) -> dict[str, Any]:
    """构造官方 ``session.update`` 消息。"""
    input_config: dict[str, Any] = {
        "format": {
            "type": "pcm",
            "codec": "pcm_s16le",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "transcription": {
            "model": settings.model,
            "language": settings.language,
            "full_rerun_on_commit": settings.full_rerun_on_commit,
            "enable_itn": settings.enable_itn,
        },
    }
    if prompt:
        input_config["transcription"]["prompt"] = prompt
    if server_vad:
        input_config["turn_detection"] = {
            "type": "server_vad",
            "silence_duration_ms": settings.silence_duration_ms,
            "threshold": settings.vad_threshold,
        }
    return {
        "event_id": f"echo-{uuid.uuid4()}",
        "type": "session.update",
        "session": {"audio": {"input": input_config}},
    }


def build_append(audio_bytes: bytes) -> dict[str, str]:
    """构造官方 ``input_audio_buffer.append`` 消息。"""
    return {
        "event_id": f"echo-{uuid.uuid4()}",
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(audio_bytes).decode("ascii"),
    }


def build_commit() -> dict[str, str]:
    """构造官方 ``input_audio_buffer.commit`` 消息。"""
    return {
        "event_id": f"echo-{uuid.uuid4()}",
        "type": "input_audio_buffer.commit",
    }


def _error_from_message(message: dict[str, Any]) -> StepFunASRError:
    error = message.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = str(error.get("code") or "provider_error")
    detail = str(error.get("message") or code)
    retryable = code not in {"invalid_value", "missing_param", "risk_blocked"}
    return StepFunASRError(
        f"StepFun ASR error code={code}: {detail}",
        category=str(error.get("type") or "provider"),
        retryable=retryable,
    )


def _completed_text(message: dict[str, Any]) -> str:
    return str(message.get("transcript") or "").strip()


def _word_bounds(message: dict[str, Any]) -> tuple[float, float]:
    words = message.get("words") or []
    if not isinstance(words, list) or not words:
        return 0.0, 0.0
    starts = [float(item["start"]) for item in words if "start" in item]
    ends = [float(item["end"]) for item in words if "end" in item]
    return (min(starts) if starts else 0.0, max(ends) if ends else 0.0)


class StepFunASRClient:
    """单条 WebSocket ASR 会话；同一连接内不并发写帧。"""

    def __init__(
        self,
        *,
        settings: StepFunASRSettings,
        device_id: str = "default",
        source: str = "device",
        on_segment: SegmentCallback | None = None,
    ) -> None:
        self.settings = settings
        self.device_id = device_id
        self.source = source
        self.on_segment = on_segment
        self._ws: Any = None
        self._receive_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session_ready = asyncio.Event()
        self._session_error: StepFunASRError | None = None
        self._closed = False
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    async def connect(self) -> None:
        """建立连接并等待 ``session.updated``。"""
        await self._connect(prompt="", server_vad=True)

    async def _connect(self, *, prompt: str, server_vad: bool) -> None:
        """建立连接并发送会话配置。"""
        if not self.settings.api_key:
            raise StepFunASRError(
                "STEPFUN_API_KEY is not configured",
                category="config",
                retryable=False,
            )

        self._closed = False
        self._session_error = None
        self._session_ready.clear()
        try:
            self._ws = await websockets.connect(
                self.settings.ws_url,
                additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
                ping_interval=10.0,
                ping_timeout=10.0,
                open_timeout=self.settings.connect_timeout_s,
            )
        except Exception as exc:
            raise StepFunASRError(
                f"StepFun ASR connect failed: {type(exc).__name__}",
                category="connect",
            ) from exc

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name=f"stepfun-asr-recv-{self.device_id}",
        )
        try:
            await self._send_json(
                build_session_update(self.settings, prompt=prompt, server_vad=server_vad)
            )
            await asyncio.wait_for(
                self._session_ready.wait(),
                timeout=self.settings.connect_timeout_s,
            )
            if self._session_error is not None:
                raise self._session_error
        except asyncio.TimeoutError as exc:
            await self.close()
            raise StepFunASRError(
                "StepFun ASR session.updated timeout",
                category="timeout",
            ) from exc
        except StepFunASRError:
            await self.close()
            raise
        self._connected = True
        logger.info(
            "[ASR][stepfun] connected model={} device={} source={}",
            self.settings.model,
            self.device_id,
            self.source,
        )

    async def push_audio(self, audio_bytes: bytes) -> None:
        """追加 raw PCM 16kHz/16-bit/mono 音频。"""
        if not audio_bytes:
            return
        if not self.is_connected:
            raise StepFunASRError("StepFun ASR session is not connected", category="connect")
        await self._send_json(build_append(audio_bytes))

    async def commit(self) -> None:
        """提交当前音频缓冲区并等待完成事件。"""
        if not self.is_connected:
            raise StepFunASRError("StepFun ASR session is not connected", category="connect")
        await self._send_json(build_commit())

    async def wait_for_completed(self) -> str:
        """等待最终转录；忽略增量和 VAD 状态事件。"""
        try:
            while True:
                message = await asyncio.wait_for(
                    self._events.get(),
                    timeout=self.settings.response_timeout_s,
                )
                message_type = message.get("type")
                if message_type == "error":
                    raise _error_from_message(message)
                if message_type == "conversation.item.input_audio_transcription.completed":
                    return _completed_text(message)
        except asyncio.TimeoutError as exc:
            raise StepFunASRError(
                "StepFun ASR completed event timeout",
                category="timeout",
            ) from exc

    async def close(self) -> None:
        """关闭会话并取消接收任务。"""
        self._closed = True
        self._connected = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.debug("[ASR][stepfun] close failed type={}", type(exc).__name__)
        self._ws = None

    async def _send_json(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await self._ws.send(json.dumps(message, ensure_ascii=False))
            except Exception as exc:
                self._connected = False
                raise StepFunASRError(
                    f"StepFun ASR send failed: {type(exc).__name__}",
                    category="send",
                ) from exc

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw if isinstance(raw, str) else raw.decode())
                except (TypeError, ValueError) as exc:
                    await self._events.put({
                        "type": "error",
                        "error": {"type": "protocol", "message": "invalid JSON"},
                    })
                    logger.warning("[ASR][stepfun] invalid JSON event type={}", type(exc).__name__)
                    continue
                await self._handle_event(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            if not self._session_ready.is_set():
                self._session_error = StepFunASRError(
                    f"StepFun ASR receive failed: {type(exc).__name__}",
                    category="receive",
                )
                self._session_ready.set()
            await self._events.put({
                "type": "error",
                "error": {"type": "connect", "message": type(exc).__name__},
            })
            if not self._closed:
                logger.warning("[ASR][stepfun] receive loop failed type={}", type(exc).__name__)

    async def _handle_event(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "session.updated":
            self._session_ready.set()
            return
        if message_type == "error":
            if not self._session_ready.is_set():
                self._session_error = _error_from_message(message)
                self._session_ready.set()
                return
            await self._events.put(message)
            return
        if message_type == "conversation.item.input_audio_transcription.completed":
            await self._events.put(message)
            if self.on_segment and _completed_text(message):
                asyncio.create_task(
                    self._emit_segment(message),
                    name=f"stepfun-asr-segment-{self.device_id}",
                )

    async def _emit_segment(self, message: dict[str, Any]) -> None:
        start, end = _word_bounds(message)
        segment = TranscriptSegment(
            text=_completed_text(message),
            confidence=1.0,
            start=start,
            end=end,
            source=self.source,
            device_id=self.device_id,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            await self.on_segment(segment)
        except Exception as exc:
            logger.exception(
                "[ASR][stepfun] on_segment callback failed type={}",
                type(exc).__name__,
            )


async def transcribe_once(
    audio_bytes: bytes,
    settings: StepFunASRSettings,
    prompt: str = "",
) -> str:
    """以流式协议提交完整 utterance，返回最终文本。"""
    client = StepFunASRClient(settings=settings)
    try:
        await client._connect(prompt=prompt, server_vad=False)
        await client.push_audio(audio_bytes)
        await client.commit()
        return await client.wait_for_completed()
    finally:
        await client.close()
