"""音频预过滤工具：RMS + 帧级 VAD + STT 字符速率门控 + VAD 句级切片。

移植自 echo backend/app/pipeline.py 的 handle_audio 预过滤逻辑（已被生产验证）。

设计意图：
- ambient 全天候采集会把环境底噪一起送进来；如果不过滤，STT 会在静音/噪声上幻觉，
  diarizer 会把每段噪声当成不同的"新说话人"——这正是当前 echo-demo 出现 61 个
  speaker、转写满是 "嗯。" "ですね" 的根因。
- echo 的预过滤在 1 年生产里已验证：先 RMS 粗过滤、再帧级 VAD 精过滤、最后字符速率门控。
- 本模块只做静态判断函数，无状态、可单独单测。

VAD 句级切片（PR echodesk-spk-2 新增）：
- 单个 6s ambient chunk 内若发生说话人切换（A 说 3s → 静 0.5s → B 说 2.5s），
  老链路会把整段做一次 ECAPA embedding，得到的是混合向量 → 跟 A、B 谁都不像 →
  被判为新说话人 → speaker explosion 的关键源头之一（ARCH-AUDIT §4 root #5b）。
- 新增 `split_into_voiced_segments` 在 STT 通过后、diarize 之前把整段切成多个连续
  voiced 段，每段独立 embed + match。
- 算法对齐 echo `backend/app/pipeline.py` 的 silence-gap 切句逻辑（200ms 间隔）。
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
    # 只统计完整帧。旧实现用 ``n - frame_samples`` 且 range 右边界不包含，
    # 每个 chunk 都会漏掉最后一帧；短音频甚至会错误返回 0。
    total = n // frame_samples
    if total <= 0:
        return 0.0
    active = 0
    for frame_index in range(total):
        i = frame_index * frame_samples
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
        return GateDecision(pass_=False, reason="speech_ratio_too_low", rms=rms, speech_ratio=ratio)
    return GateDecision(pass_=True, reason="ok", rms=rms, speech_ratio=ratio)


def normalize_transcript_text(text: str) -> str:
    """生成用于幻觉/重复判断的稳定文本签名。

    仅保留 Unicode 字母和数字，并统一大小写。这样 ``“嗯……”`` 不会因为
    标点数量达到 ``min_chars`` 而绕过短文本门，跨 chunk 的标点差异也不会
    绕过重复检测。
    """

    return "".join(char.casefold() for char in text if char.isalnum())


def _repetitive_text_reason(normalized: str) -> str | None:
    """识别短周期复读，不把普通的少量重复措辞误判为幻觉。"""

    n_chars = len(normalized)
    if n_chars < 5:
        return None
    if len(set(normalized)) == 1:
        return "single_char"

    # 至少完整重复 3 次，且周期模板覆盖率 ≥ 90% 才拒绝。例如
    # “嗯嗯嗯…”、“测试测试测试”会命中，“非常非常重要”不会命中。
    for unit_size in range(1, min(12, n_chars // 3) + 1):
        unit = normalized[:unit_size]
        template = (unit * ((n_chars + unit_size - 1) // unit_size))[:n_chars]
        matches = sum(left == right for left, right in zip(normalized, template, strict=True))
        if matches / n_chars >= 0.9:
            return f"periodic_unit_{unit_size}"

    # 长文本的 3-gram 多样性极低，也通常是 ASR 卡住后的循环输出。
    if n_chars >= 12:
        trigrams = [normalized[i : i + 3] for i in range(n_chars - 2)]
        if len(set(trigrams)) / len(trigrams) <= 0.25:
            return "low_ngram_diversity"
    return None


def is_likely_hallucination(
    text: str,
    audio_bytes: bytes,
    *,
    max_cps: float = 12.0,
    min_chars: int = 4,
    speech_duration_s: float | None = None,
) -> tuple[bool, str]:
    """STT 后置过滤：有效字符、复读模式与字符速率。

    - 有效字符短于 min_chars 直接判幻觉，标点不计入长度
    - 短周期复读或极低 n-gram 多样性直接判幻觉
    - 常规 cps 仍按完整音频时长计算；简单 RMS-VAD 会漏掉清辅音和停顿，直接按
      active time 会误杀真实快语速
    - 调用方提供 ``speech_duration_s`` 时，额外拦截 active-cps 极端异常值
    - cps > max_cps → 视为复读/幻觉

    返回 (is_hallu, reason)。
    """
    t = text.strip()
    normalized = normalize_transcript_text(t)
    if len(normalized) < min_chars:
        return True, f"too_short({len(normalized)}<{min_chars})"

    repetition_reason = _repetitive_text_reason(normalized)
    if repetition_reason is not None:
        return True, f"repetitive_text({repetition_reason})"

    audio_duration_s = len(audio_bytes) / (_SAMPLE_RATE * 2)  # int16 mono = 32000 B/s
    if audio_duration_s >= 3.0 and len(normalized) >= 12:
        cps = len(normalized) / audio_duration_s
        if cps > max_cps:
            return True, f"cps_too_high({cps:.1f}>{max_cps})"

    # 真实保存 WAV 的只读抽样显示，简单能量 VAD 下正常中文 active-cps 可到
    # 20 左右，因此不能直接套用常规阈值。这里只拒绝高出常规上限 2.5 倍的
    # 极端结果，用来抓“不到 1s 活跃噪声却返回几十字”的典型幻觉。
    if speech_duration_s is not None and speech_duration_s >= 0.8 and len(normalized) >= 12:
        active_duration_s = min(audio_duration_s, speech_duration_s)
        active_cps = len(normalized) / active_duration_s
        active_cps_limit = max_cps * 2.5
        if active_cps > active_cps_limit:
            return True, f"active_cps_too_high({active_cps:.1f}>{active_cps_limit:.1f})"
    return False, "ok"


@dataclass(slots=True, frozen=True)
class VoicedSegment:
    """一段连续 voiced 区间。

    - start_ms / end_ms：相对整段输入的偏移（int16 mono 16kHz）
    - audio_bytes：该段在原 buffer 中切出的 PCM
    - active_ratio：段内活跃帧占比（diarizer 阈值门控用）
    """

    start_ms: int
    end_ms: int
    audio_bytes: bytes
    active_ratio: float

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def split_into_voiced_segments(
    audio_bytes: bytes,
    *,
    frame_ms: int = 20,
    frame_rms_threshold: int = 400,
    min_segment_ms: int = 800,
    max_silence_gap_ms: int = 200,
) -> list[VoicedSegment]:
    """把整段音频切成多个连续 voiced 段（按 silence-gap 分句）。

    算法（与 echo 对齐）：
    1. 按 frame_ms 切帧，每帧算 RMS → active=(rms > frame_rms_threshold)
    2. 状态机扫一遍：
       - 静音态见到 active → 进入 voiced（标 start）
       - voiced 态见到 active → 继续，silence_run=0
       - voiced 态见到 silent → silence_run+=1，若 silence_run*frame_ms > max_silence_gap_ms
         → 退出 voiced，end = 最后一个 active 帧尾，emit
    3. 段长 < min_segment_ms 丢弃（embed 不可靠，echo 的 _MIN_BYTES_FOR_EMBED 也是 1s）

    边界：
    - 整段全静音 → 返回 []
    - 整段全活跃 → 返回 1 段（覆盖完整 buffer）
    - 末尾未结束的 voiced 段也会被 emit（用末帧位置作为 end）

    返回的 audio_bytes 是 raw int16 PCM 切片（不包 WAV header），调用方自己加。
    """
    if frame_ms <= 0 or frame_rms_threshold < 0:
        return []
    n_samples = len(audio_bytes) // 2
    frame_samples = int(_SAMPLE_RATE * frame_ms / 1000)
    if frame_samples <= 0 or n_samples < frame_samples:
        return []
    samples = struct.unpack_from(f"<{n_samples}h", audio_bytes)

    total_frames = n_samples // frame_samples
    if total_frames == 0:
        return []

    actives: list[bool] = []
    for fi in range(total_frames):
        chunk = samples[fi * frame_samples : (fi + 1) * frame_samples]
        rms = math.sqrt(sum(s * s for s in chunk) / frame_samples)
        actives.append(rms > frame_rms_threshold)

    max_silence_frames = max(1, max_silence_gap_ms // frame_ms)
    min_seg_frames = max(1, min_segment_ms // frame_ms)

    segments: list[VoicedSegment] = []
    in_voice = False
    start_f = 0
    last_active_f = 0
    silence_run = 0

    def _emit(start_frame: int, end_frame_exclusive: int) -> None:
        if end_frame_exclusive - start_frame < min_seg_frames:
            return
        s_byte = start_frame * frame_samples * 2
        e_byte = end_frame_exclusive * frame_samples * 2
        seg_bytes = audio_bytes[s_byte:e_byte]
        n_active = sum(1 for f in range(start_frame, end_frame_exclusive) if actives[f])
        ratio = n_active / max(1, end_frame_exclusive - start_frame)
        segments.append(
            VoicedSegment(
                start_ms=start_frame * frame_ms,
                end_ms=end_frame_exclusive * frame_ms,
                audio_bytes=seg_bytes,
                active_ratio=ratio,
            )
        )

    for fi, is_active in enumerate(actives):
        if is_active:
            if not in_voice:
                in_voice = True
                start_f = fi
            last_active_f = fi
            silence_run = 0
        elif in_voice:
            silence_run += 1
            if silence_run >= max_silence_frames:
                _emit(start_f, last_active_f + 1)
                in_voice = False
                silence_run = 0
    if in_voice:
        _emit(start_f, last_active_f + 1)

    return segments


__all__ = [
    "GateDecision",
    "VoicedSegment",
    "integer_rms",
    "is_likely_hallucination",
    "normalize_transcript_text",
    "pre_stt_gate",
    "speech_frame_ratio",
    "split_into_voiced_segments",
]
