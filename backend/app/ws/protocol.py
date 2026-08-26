"""
设备 ↔ 后端 binary WebSocket 帧协议（v2）

帧格式（3 字节头 + payload）：
  Byte 0   : 消息类型（FrameType）
  Byte 1-2 : payload 长度，big-endian uint16
  Byte 3+  : payload（音频或 JSON）

文本帧（JSON）由 FastAPI WebSocket 原生处理，
binary 帧只负责音频和信令。
"""
from __future__ import annotations

import struct
from enum import IntEnum


class FrameType(IntEnum):
    AUDIO        = 0x01  # 双向：raw PCM 16kHz mono int16 LE
    STATE        = 0x02  # 后端→设备：设备状态命令（1 字节 payload = DeviceState）
    AUDIO_END    = 0x03  # 保留兼容（已废弃，设备不再发送，后端仍可接收）
    AUDIO_CANCEL = 0x04  # 后端→设备：打断当前播放
    PING         = 0x10  # 设备→后端
    PONG         = 0x11  # 后端→设备


class DeviceState(IntEnum):
    """后端→设备的状态命令（STATE 帧 payload 的 1 字节值）。"""
    IDLE      = 0x00  # 静默，停止发上行音频
    LISTENING = 0x01  # 开始发上行音频
    THINKING  = 0x02  # LLM 处理中，显示转圈，停发上行
    SPEAKING  = 0x03  # TTS 播放中，停发上行音频


class FrameDecodeError(ValueError):
    """帧解码失败（数据截断、非法类型等）。"""


_HEADER_SIZE = 3
_MAX_PAYLOAD = 64 * 1024  # 64KB 安全上限（C3 ring buffer 大小）


def encode_frame(frame_type: FrameType, payload: bytes = b"") -> bytes:
    """将 frame_type + payload 编码为 binary 帧字节串。"""
    if len(payload) > _MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)}B 超过 {_MAX_PAYLOAD}B 上限")
    return bytes([frame_type.value]) + struct.pack(">H", len(payload)) + payload


def encode_state_frame(state: DeviceState) -> bytes:
    """编码 STATE 帧（1 字节 payload = state code）。"""
    return encode_frame(FrameType.STATE, bytes([state.value]))


def decode_frame(data: bytes) -> tuple[FrameType, bytes]:
    """
    解码 binary 帧，返回 (FrameType, payload)。
    数据不足或类型非法时抛出 FrameDecodeError。
    """
    if len(data) < _HEADER_SIZE:
        raise FrameDecodeError(f"帧过短：{len(data)} < {_HEADER_SIZE}")

    raw_type = data[0]
    try:
        ftype = FrameType(raw_type)
    except ValueError:
        raise FrameDecodeError(f"未知帧类型 0x{raw_type:02x}")

    (length,) = struct.unpack(">H", data[1:3])
    if len(data) < _HEADER_SIZE + length:
        raise FrameDecodeError(
            f"payload 不完整：期望 {length}B，实际 {len(data) - _HEADER_SIZE}B"
        )

    return ftype, data[_HEADER_SIZE : _HEADER_SIZE + length]
