"""音频格式转换：PCM/WAV/float numpy，纯函数实现（仅 stdlib + numpy）。"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class NormalizedAudio:
    """Capture/meeting pipeline 内部统一使用的音频格式。

    `pcm` 始终是 raw int16 little-endian mono；`sample_rate` 是该 PCM 的采样率。
    前端和 Android 原生插件上传的是 WAV 容器，旧测试/脚本可能仍上传 raw PCM，
    所以入口需要同时兼容两种格式。
    """

    pcm: bytes
    sample_rate: int


def is_wav_bytes(data: bytes) -> bool:
    """快速判断一段 bytes 是否是 RIFF/WAVE 容器。"""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def pcm_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """把裸 PCM 字节封装成 WAV 容器（默认 16kHz 16bit mono）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def wav_to_pcm16_mono(
    wav_bytes: bytes,
    *,
    target_sample_rate: int | None = 16_000,
) -> NormalizedAudio:
    """把 WAV 容器解成 raw int16 mono PCM。

    EchoDesk 前端上传的 chunk 已经是 16k/16bit/mono，本函数的热路径不需要 numpy。
    对少数 8bit/32bit/stereo/非 16k 的输入，用 numpy 做简单平均和线性重采样，
    主要用于兼容 Android 设备厂商返回的非标准参数。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        src_rate = wf.getframerate()
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if not raw:
        return NormalizedAudio(pcm=b"", sample_rate=target_sample_rate or src_rate)

    if (
        sampwidth == 2
        and nchannels == 1
        and (target_sample_rate is None or src_rate == target_sample_rate)
    ):
        return NormalizedAudio(pcm=raw, sample_rate=src_rate)

    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise ValueError("numpy is required to normalize non-16bit-mono WAV") from e

    if sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 65536.0
    elif sampwidth == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) * 256.0
    else:
        raise ValueError(f"unsupported sample width: {sampwidth}")

    if nchannels > 1:
        arr = arr.reshape(-1, nchannels).mean(axis=1)

    out_rate = target_sample_rate or src_rate
    if src_rate != out_rate:
        ratio = out_rate / src_rate
        n_out = int(len(arr) * ratio)
        if n_out <= 0:
            return NormalizedAudio(pcm=b"", sample_rate=out_rate)
        x = np.linspace(0.0, len(arr) - 1, n_out, dtype=np.float32)
        idx0 = x.astype(np.int32)
        idx1 = np.minimum(idx0 + 1, len(arr) - 1)
        frac = x - idx0.astype(np.float32)
        arr = arr[idx0] * (1 - frac) + arr[idx1] * frac

    arr_i16 = np.clip(arr, -32768, 32767).astype("<i2")
    return NormalizedAudio(pcm=arr_i16.tobytes(), sample_rate=out_rate)


def normalize_audio_bytes(audio_bytes: bytes, *, sample_rate: int = 16_000) -> NormalizedAudio:
    """兼容前端 WAV 上传和旧 raw PCM 调用，返回 pipeline 标准 PCM。"""
    if is_wav_bytes(audio_bytes):
        return wav_to_pcm16_mono(audio_bytes, target_sample_rate=sample_rate)
    return NormalizedAudio(pcm=audio_bytes, sample_rate=sample_rate)


def wav_to_float_mono16k(wav_bytes: bytes):  # type: ignore[no-untyped-def]
    """把 WAV 字节解析成 (float32 [-1,1] numpy 数组, 16kHz)。

    依赖 numpy；缺少 numpy 时返回 None。
    自动重采样到 16kHz，自动 stereo→mono 平均。
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        src_rate = wf.getframerate()
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if not raw:
        return np.zeros(0, dtype=np.float32)

    if sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2_147_483_648.0
    elif sampwidth == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width: {sampwidth}")

    if nchannels == 2:
        arr = arr.reshape(-1, 2).mean(axis=1)

    if src_rate != 16_000:
        # 线性插值重采样（音频质量足够用于 STT/声纹）
        ratio = 16_000 / src_rate
        n_out = int(len(arr) * ratio)
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        x = np.linspace(0.0, len(arr) - 1, n_out, dtype=np.float32)
        idx0 = x.astype(np.int32)
        idx1 = np.minimum(idx0 + 1, len(arr) - 1)
        frac = x - idx0.astype(np.float32)
        arr = arr[idx0] * (1 - frac) + arr[idx1] * frac

    return arr.astype(np.float32)
