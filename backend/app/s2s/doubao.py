"""
Doubao 端到端实时语音对话 API 客户端

官方协议参考：https://www.volcengine.com/docs/6561/1594356
SDK 参考实现：github.com/MarkShawn2020/realtime-dialog

帧格式（big-endian）：
  Byte 0: version(4) | header_size(4)   固定 0x11
  Byte 1: msg_type(4) | flags(4)
  Byte 2: serialization(4) | compress(4)
  Byte 3: reserved  固定 0x00
  -- Body --
  [event_id: 4B]          当 flags & 0b0100
  [session_id_len: 4B]    有 session_id 时
  [session_id: N B]
  [payload_len: 4B]
  [payload: gzip(json) or gzip(pcm)]

鉴权方式（HTTP Upgrade Headers）：
  X-Api-App-ID:       DOUBAO_REALTIME_APP_ID
  X-Api-Access-Key:   DOUBAO_REALTIME_ACCESS_KEY  (旧版 Token)
  X-Api-Resource-Id:  volc.speech.dialog  (固定)
  X-Api-App-Key:      PlgvMymc7f3tQnJ6    (固定)
  X-Api-Connect-Id:   uuid4() per connection
"""
from __future__ import annotations

import asyncio
import gzip
import json
import struct
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from loguru import logger

# ── 协议常量 ──────────────────────────────────────────────────────────────────

_VERSION              = 0b0001
_HEADER_SIZE_UNITS    = 0b0001   # header = 1 × 4 = 4 bytes

# Message Type（高 4 位 Byte 1）
_MSG_FULL_REQUEST     = 0b0001   # JSON 控制帧（客户端→服务端）
_MSG_AUDIO_REQUEST    = 0b0010   # 音频帧（客户端→服务端）
_MSG_FULL_RESPONSE    = 0b1001   # JSON 响应（服务端→客户端）
_MSG_AUDIO_RESPONSE   = 0b1011   # 音频响应（服务端→客户端）
_MSG_ERROR            = 0b1111   # 错误

# Flags（低 4 位 Byte 1）
_FLAG_WITH_EVENT      = 0b0100   # 帧携带 event_id

# Byte 2
_SERIAL_JSON          = 0b0001
_COMPRESS_GZIP        = 0b0001

# 事件 ID（客户端发送）
class EventSend:
    StartConnection  = 1
    FinishConnection = 2
    StartSession     = 100
    FinishSession    = 102
    SendAudio        = 200   # 音频帧
    ChatTextQuery    = 501   # 文字提问

# 事件 ID（服务端返回）
class EventReceive:
    ConnectionStarted = 50
    ConnectionFailed  = 51
    ConnectionFinished = 52
    SessionStarted    = 150
    SessionFailed     = 153
    TTSResponse       = 352   # 服务端 TTS 音频帧（AUDIO_ONLY_RESPONSE）
    TTSEnded          = 359
    ASRInfo           = 450   # VAD 检测到说话开始
    ASREnded          = 459   # VAD 静音结束
    ChatResponse      = 550
    ChatEnded         = 559
    DialogError       = 599

# 固定 Header 常量
_FIXED_RESOURCE_ID = "volc.speech.dialog"
_FIXED_APP_KEY     = "PlgvMymc7f3tQnJ6"
_WS_URL            = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"


# ── 帧构建 ────────────────────────────────────────────────────────────────────

def _make_header(msg_type: int, flags: int = _FLAG_WITH_EVENT,
                 serial: int = _SERIAL_JSON,
                 compress: int = _COMPRESS_GZIP) -> bytes:
    h = bytearray(4)
    h[0] = (_VERSION << 4) | _HEADER_SIZE_UNITS
    h[1] = (msg_type << 4) | flags
    h[2] = (serial << 4) | compress
    h[3] = 0x00
    return bytes(h)


def _build_frame(event_id: int,
                 session_id: str | None,
                 payload: dict | bytes,
                 audio_only: bool = False) -> bytes:
    """
    构造一个完整的客户端帧。
    - payload 为 dict  → JSON 序列化 + gzip
    - payload 为 bytes → 原始 PCM，直接 gzip（audio_only=True）
    """
    if audio_only:
        hdr  = _make_header(_MSG_AUDIO_REQUEST)
        body = gzip.compress(payload)          # PCM gzip 压缩
    else:
        hdr  = _make_header(_MSG_FULL_REQUEST)
        body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())

    frame = bytearray(hdr)
    frame += struct.pack(">I", event_id)

    if session_id:
        sid = session_id.encode()
        frame += struct.pack(">I", len(sid))
        frame += sid

    frame += struct.pack(">I", len(body))
    frame += body
    return bytes(frame)


# ── 服务端帧解析 ───────────────────────────────────────────────────────────────

@dataclass
class DoubaoFrame:
    msg_type:   int
    event_id:   int | None
    session_id: str | None
    payload:    bytes   # 解压后的 payload（PCM 或 JSON bytes）


def _parse_frame(data: bytes) -> DoubaoFrame:
    """
    解析 Doubao 服务端响应帧。

    服务端帧格式：
        [header 4B]
        [event_id 4B]         当 flags & 0b0100 时
        [session_id_len 4B]   可选；值 ≤ 256 时跳过对应字节
        [session_id N B]      当 session_id_len ∈ (0, 256] 时
        [payload_len 4B]
        [payload N B]         JSON（gzip）或裸 PCM

    Doubao 服务端 TTSResponse 帧确实携带 session_id（UUID 36 字节）。
    session_id_len 字段的值 36 = 0x00000024 是合法的小正整数，
    必须先跳过 session_id 才能正确定位 payload_len。

    判断 session_id 是否存在的启发式规则：
        - session_id_len 为无符号 32-bit 整数
        - 若其值 ≤ 512，视为合法 session_id 长度，读取并跳过
        - 若其值 > 512，认为此字段不存在（格式变体），当作 payload_len 处理
    """
    if len(data) < 4:
        raise ValueError(f"帧过短：{len(data)}")

    msg_type  = (data[1] >> 4) & 0xF
    flags     = data[1] & 0xF
    compress  = data[2] & 0xF
    hdr_bytes = (data[0] & 0xF) * 4   # header_size × 4

    pos        = hdr_bytes
    event_id   = None
    session_id = None

    if flags & _FLAG_WITH_EVENT:
        if pos + 4 > len(data):
            raise ValueError("缺少 event_id")
        event_id = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4

    # session_id 字段（可选）
    # 用启发式：若接下来 4 字节 ≤ 512 且后续有足够空间，视为 session_id_len
    raw = b""
    if pos + 4 <= len(data):
        maybe_sid_len = struct.unpack(">I", data[pos:pos + 4])[0]
        if maybe_sid_len <= 512 and pos + 4 + maybe_sid_len + 4 <= len(data):
            # 合法的 session_id：跳过
            pos += 4
            if maybe_sid_len > 0:
                session_id = data[pos:pos + maybe_sid_len].decode(errors="replace")
                pos += maybe_sid_len
        # else: maybe_sid_len 是 payload_len（无 session_id 字段格式）

        # 读取 payload_len + payload
        if pos + 4 <= len(data):
            pay_len = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
            raw = data[pos:pos + pay_len]

    if compress == _COMPRESS_GZIP and raw:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass   # 音频帧通常不压缩，JSON 帧才压缩

    return DoubaoFrame(
        msg_type=msg_type,
        event_id=event_id,
        session_id=session_id,
        payload=raw,
    )


# ── 会话配置 ──────────────────────────────────────────────────────────────────

@dataclass
class DoubaoSessionConfig:
    app_id:       str
    access_key:   str          # X-Api-Access-Key（控制台 Token / Access Key）
    bot_name:     str = "Echo"
    system_role:  str = "你是一个友好的语音助手。"
    speaking_style: str = ""
    voice_type:   str = "zh_female_vv_jupiter_bigtts"
    input_sample_rate:  int = 16000
    output_sample_rate: int = 16000   # 与 ESP32-C3 ES8311 I2S 配置一致
    model: str = "1.2.1.1"


# ── Doubao Realtime 客户端 ────────────────────────────────────────────────────

_RECONNECT_DELAY_S  = 3.0
_MAX_RECONNECT      = 3
_RESPONSE_TIMEOUT_S = 12.0


@dataclass
class DoubaoRealtimeClient:
    """
    Doubao 端到端实时语音对话 WebSocket 客户端（单会话）。

    用法：
        async with DoubaoRealtimeClient(cfg) as client:
            await client.send_audio(pcm_bytes)
            async for frm in client.events():
                if frm.event_id == EventReceive.TTSResponse:
                    play(frm.payload)
    """
    cfg:        DoubaoSessionConfig
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    _ws:              Any  = field(default=None, init=False, repr=False)
    _connected:       bool = field(default=False, init=False)
    _session_started: bool = field(default=False, init=False)
    _session_ready:   asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _event_queue: asyncio.Queue = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )

    async def __aenter__(self) -> "DoubaoRealtimeClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    @property
    def is_ready(self) -> bool:
        return self._connected and self._session_started

    async def connect(self) -> None:
        connect_id = str(uuid.uuid4())
        headers = {
            "X-Api-App-ID":      self.cfg.app_id,
            "X-Api-Access-Key":  self.cfg.access_key,
            "X-Api-Resource-Id": _FIXED_RESOURCE_ID,
            "X-Api-App-Key":     _FIXED_APP_KEY,
            "X-Api-Connect-Id":  connect_id,
        }
        logger.info(
            f"[Doubao] 连接 {_WS_URL} "
            f"app={self.cfg.app_id} session={self.session_id[:8]}…"
        )
        # websockets >= 14.0 用 additional_headers，< 14.0 用 extra_headers
        import websockets
        _ws_ver = tuple(int(x) for x in websockets.__version__.split(".")[:2])
        _headers_kwarg = "additional_headers" if _ws_ver >= (14, 0) else "extra_headers"
        self._ws = await websockets.connect(
            _WS_URL,
            **{_headers_kwarg: headers},
            ping_interval=None,
            open_timeout=15,
        )
        self._connected = True

        # 1. StartConnection（无 session_id）
        frame = _build_frame(EventSend.StartConnection, None, {})
        await self._ws.send(frame)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        frm = _parse_frame(raw if isinstance(raw, bytes) else raw.encode())
        if frm.event_id != EventReceive.ConnectionStarted:
            raise ConnectionError(
                f"[Doubao] 连接失败，event_id={frm.event_id} payload={frm.payload[:200]}"
            )
        logger.info("[Doubao] ConnectionStarted ✓")

        # 2. StartSession
        await self._start_session()

        # 3. 启动后台接收任务，等待 SessionStarted 再返回
        asyncio.create_task(
            self._recv_loop(),
            name=f"doubao-recv-{self.session_id[:8]}"
        )
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise ConnectionError("[Doubao] 等待 SessionStarted 超时（10s）")

    async def _start_session(self) -> None:
        config: dict[str, Any] = {
            "asr": {
                "extra": {
                    "end_smooth_window_ms": 800,   # 800ms 静音结束窗口，减少 TTFA
                },
            },
            "tts": {
                "speaker": self.cfg.voice_type,
                "audio_config": {
                    "channel":     1,
                    "format":      "pcm",
                    "sample_rate": self.cfg.output_sample_rate,
                },
            },
            "dialog": {
                "bot_name":   self.cfg.bot_name,
                "system_role": self.cfg.system_role,
                "extra": {
                    "input_mod": "audio",
                },
            },
        }

        frame = _build_frame(EventSend.StartSession, self.session_id, config)
        await self._ws.send(frame)
        logger.debug(f"[Doubao] StartSession 已发送 session={self.session_id[:8]}")

    async def _recv_loop(self) -> None:
        """后台任务：持续接收服务端帧并放入事件队列。"""
        try:
            async for raw in self._ws:
                try:
                    data = raw if isinstance(raw, bytes) else raw.encode()
                    # 诊断：打印每帧原始头 + 长度
                    logger.info(
                        f"[Doubao] ← 收到帧 {len(data)}B "
                        f"hex={data[:16].hex()}"
                    )
                    frm  = _parse_frame(data)
                    logger.info(
                        f"[Doubao] ← 解析结果 msg_type={frm.msg_type} "
                        f"event_id={frm.event_id} payload={len(frm.payload)}B"
                    )
                    await self._event_queue.put(frm)

                    if frm.event_id == EventReceive.SessionStarted:
                        self._session_started = True
                        self._session_ready.set()
                        logger.info("[Doubao] SessionStarted ✓")
                    elif frm.msg_type == _MSG_ERROR:
                        # 服务端错误帧（msg_type=15），通常 event_id=None
                        # finally 块会自动发送 None 哨兵通知 session 重连
                        try:
                            err_raw = frm.payload.lstrip(b"\x00")
                            err = json.loads(err_raw)
                        except Exception:
                            err = frm.payload[:200]
                        logger.error(f"[Doubao] 服务端 ERROR 帧: {err}")
                        self._connected       = False
                        self._session_started = False
                        return
                    elif frm.event_id in (
                        EventReceive.SessionFailed,
                        EventReceive.ConnectionFailed,
                        EventReceive.DialogError,
                    ):
                        try:
                            err = json.loads(frm.payload)
                        except Exception:
                            err = frm.payload.decode(errors="replace")
                        logger.error(f"[Doubao] 错误 event_id={frm.event_id}: {err}")
                except Exception as e:
                    logger.error(f"[Doubao] 帧解析异常: {e} raw={data[:32].hex() if 'data' in dir() else '?'}")

        except websockets.ConnectionClosed as e:
            logger.warning(f"[Doubao] WS 关闭: {e.code} {e.reason}")
        finally:
            self._connected       = False
            self._session_started = False
            await self._event_queue.put(None)   # 哨兵：通知 events() 退出

    async def send_audio(self, pcm: bytes) -> None:
        """发送 PCM 音频帧到 Doubao（事件 200，gzip 压缩）。"""
        if not self._connected:
            return
        frame = _build_frame(
            EventSend.SendAudio, self.session_id, pcm, audio_only=True
        )
        try:
            await self._ws.send(frame)
        except websockets.ConnectionClosed:
            logger.warning("[Doubao] send_audio: 连接已关闭")
            self._connected = False

    async def send_text(self, text: str) -> None:
        """发送 ChatTextQuery（事件 501），让 Doubao 直接生成 TTS。"""
        if not self._connected:
            return
        payload = {"text": text}
        frame = _build_frame(EventSend.ChatTextQuery, self.session_id, payload)
        try:
            await self._ws.send(frame)
            logger.info(f"[Doubao] ChatTextQuery 已发送: {text[:60]}")
        except websockets.ConnectionClosed:
            logger.warning("[Doubao] send_text: 连接已关闭")
            self._connected = False

    async def cancel(self) -> None:
        """发 FinishSession 打断当前对话（barge-in）。"""
        if not self._connected:
            return
        try:
            frame = _build_frame(EventSend.FinishSession, self.session_id, {})
            await self._ws.send(frame)
            logger.info("[Doubao] FinishSession（cancel）已发送")
        except Exception:
            pass

    async def close(self) -> None:
        if not self._connected or not self._ws:
            return
        try:
            frame = _build_frame(EventSend.FinishSession, self.session_id, {})
            await self._ws.send(frame)
            fc = _build_frame(EventSend.FinishConnection, None, {})
            await self._ws.send(fc)
            await self._ws.close()
        except Exception:
            pass
        self._connected       = False
        self._session_started = False
        self._session_ready.clear()
        logger.info(f"[Doubao] 会话关闭 session={self.session_id[:8]}")

    async def events(self) -> AsyncIterator[DoubaoFrame]:
        """异步迭代器：逐帧产出服务端事件，直到连接关闭。"""
        while True:
            frm = await self._event_queue.get()
            if frm is None:
                return
            yield frm
