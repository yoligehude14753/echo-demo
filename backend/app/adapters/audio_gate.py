"""音频预过滤工具：RMS + 帧级 VAD + STT 字符速率门控。

移植自 echo backend/app/pipeline.py 的 handle_audio 预过滤逻辑（已被生产验证）。

设计意图：
- ambient 全天候采集会把环境底噪一起送进来；如果不过滤，STT 会在静音/噪声上幻觉，
  diarizer 会把每段噪声当成不同的"新说话人"——这正是当前 echo-demo 出现 61 个
  speaker、转写满是 "嗯。" "ですね" 的根因。
- echo 的预过滤在 1 年生产里已验证：先 RMS 粗过滤、再帧级 VAD 精过滤、最后字符速率门控。
- 本模块只做静态判断函数，无状态、可单独单测。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

_SAMPLE_RATE = 16_000  # 全链路约定 16kHz int16 mono


@dataclass(slots=True)
class GateDecision:
    """前置门控判定结果。"""

    pass_: bool
    reason: str  # "ok" / "rms_too_low" / "speech_ratio_too_low" / ...
    rms: float = 0.0
    speech_ratio: float = 0.0


def integer_rms(audio_bytes: bytes) -> float:
    """整段音频的 int16 RMS。空缓冲 → 0。"""
    n = len(audio_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack_from(f"<{n}h", audio_bytes)
    return math.sqrt(sum(s * s for s in samples) / n)


def speech_frame_ratio(
    audio_bytes: bytes,
    *,
    frame_ms: int = 20,
    frame_rms_threshold: int = 400,
) -> float:
    """20ms 帧级活跃率：活跃帧数 / 总帧数。

    一个帧"活跃" = 帧 RMS > frame_rms_threshold。
    适合识别"大缓冲区里只有 0.5s 偶发噪声、整段 RMS 又勉强过线"的伪语音场景。
    """
    n = len(audio_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack_from(f"<{n}h", audio_bytes)
    frame_samples = int(_SAMPLE_RATE * frame_ms / 1000)
    if frame_samples <= 0 or n < frame_samples:
        return 0.0
    total = (n - frame_samples) // frame_samples
    if total <= 0:
        return 0.0
    active = 0
    for i in range(0, n - frame_samples, frame_samples):
        chunk = samples[i : i + frame_samples]
        rms = math.sqrt(sum(s * s for s in chunk) / frame_samples)
        if rms > frame_rms_threshold:
            active += 1
    return active / total


def pre_stt_gate(
    audio_bytes: bytes,
    *,
    rms_gate: int,
    frame_rms_threshold: int,
    min_speech_frame_ratio: float,
) -> GateDecision:
    """STT 前置门控：两道关卡。

    1. 整段 RMS < rms_gate → 直接拒（静音/几乎无声）
    2. 帧级活跃率 < min_speech_frame_ratio → 拒（大段静音里偶发噪声）

    返回 GateDecision；调用方按 pass_ 决定是否跑 STT。
    """
    rms = integer_rms(audio_bytes)
    if rms < rms_gate:
        return GateDecision(pass_=False, reason="rms_too_low", rms=rms)
    ratio = speech_frame_ratio(
        audio_bytes,
        frame_rms_threshold=frame_rms_threshold,
    )
    if ratio < min_speech_frame_ratio:
        return GateDecision(
            pass_=False, reason="speech_ratio_too_low", rms=rms, speech_ratio=ratio
        )
    return GateDecision(pass_=True, reason="ok", rms=rms, speech_ratio=ratio)


def is_likely_hallucination(
    text: str,
    audio_bytes: bytes,
    *,
    max_cps: float = 12.0,
    min_chars: int = 4,
) -> tuple[bool, str]:
    """STT 后置过滤：字符速率 + 最短长度。

    - text 短于 min_chars 直接判幻觉（< 4 字大概率是 "嗯。" "ですね" 等噪声幻觉）
    - 仅对长音频（≥3s 且 ≥12 chars）跑 cps 阈值；短句 cps 天然偏高，不计
    - cps > max_cps → 视为复读/幻觉

    返回 (is_hallu, reason)。
    """
    t = text.strip()
    if len(t) < min_chars:
        return True, f"too_short({len(t)}<{min_chars})"
    duration_s = len(audio_bytes) / (_SAMPLE_RATE * 2)  # int16 mono = 32000 B/s
    if duration_s >= 3.0 and len(t) >= 12:
        cps = len(t) / duration_s
        if cps > max_cps:
            return True, f"cps_too_high({cps:.1f}>{max_cps})"
    return False, "ok"


__all__ = [
    "GateDecision",
    "integer_rms",
    "speech_frame_ratio",
    "pre_stt_gate",
    "is_likely_hallucination",
]
