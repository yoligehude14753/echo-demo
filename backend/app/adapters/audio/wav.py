"""音频格式转换：PCM/WAV/float numpy，纯函数实现（仅 stdlib + numpy）。"""

from __future__ import annotations

import io
import wave


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


def wav_to_float_mono16k(wav_bytes: bytes):  # type: ignore[no-untyped-def]
    """把 WAV 字节解析成 (float32 [-1,1] numpy 数组, 16kHz)。

    依赖 numpy；adapter 层 import 失败时返回 None。
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
