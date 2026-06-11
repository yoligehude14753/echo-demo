"""ambient pre-gate + hallucination gate 阈值收紧的回归测试（echodesk-spk-4）。

目的：把 `backend/app/config.py` 里 ambient_* 的"新默认值"在边界场景上写死成测试，
防止后续 PR（spk-2/3/5）误回退到旧 echo 基线。

测试不去碰 `audio_gate.py` 算法本体（spk-2 会扩展那里），只通过算法的纯函数
入参喂入合成的 int16 PCM，验证新阈值的取舍是否合预期。

合成音频：
- 全部用 `struct` 写 little-endian int16 PCM；不依赖 numpy / 任何外部音频库。
- 采样率 16kHz mono（与生产链路约定一致）。
- 噪声段用 LCG（线性同余）伪随机，保证测试可复现且无依赖。
"""

from __future__ import annotations

import math
import struct

from app.adapters.audio_gate import (
    integer_rms,
    is_likely_hallucination,
    pre_stt_gate,
    speech_frame_ratio,
)
from app.config import Settings

SAMPLE_RATE = 16_000


# ── 合成工具 ──────────────────────────────────────────────────────


def _pack_int16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _silence(seconds: float) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    return _pack_int16([0] * n)


def _noise(seconds: float, target_rms: int, *, seed: int = 1) -> bytes:
    """生成目标 RMS 的伪随机噪声（int16，LCG，无依赖）。

    用均匀分布 [-A, A] 取样，对应 RMS = A / sqrt(3) → A = target_rms * sqrt(3)。
    """
    n = int(seconds * SAMPLE_RATE)
    amp = int(target_rms * math.sqrt(3))
    samples: list[int] = []
    state = seed
    for _ in range(n):
        # numerical recipes LCG（参数无关紧要，只要稳定）
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        # 映射到 [-amp, amp]
        v = ((state & 0xFFFF) - 32768) * amp // 32768
        # int16 clip 保护
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        samples.append(v)
    return _pack_int16(samples)


def _sine(seconds: float, amplitude: int, freq_hz: float = 220.0) -> bytes:
    """生成稳定 sin 波，amplitude=3000 时 RMS ≈ 2121（远高于任何 noise 底）。"""
    n = int(seconds * SAMPLE_RATE)
    omega = 2.0 * math.pi * freq_hz / SAMPLE_RATE
    samples = [int(amplitude * math.sin(omega * i)) for i in range(n)]
    return _pack_int16(samples)


# ── 阈值预期（与 config.py 默认值同步）─────────────────────────────


def _new_thresholds() -> Settings:
    return Settings()


def test_new_defaults_match_expected_values() -> None:
    """阈值数字本身固化进测试，防止默认值被人无意识改回去。

    任何对这些数字的修改必须同步改本测试 + 在 PR 描述里说明依据。
    """
    s = _new_thresholds()
    assert s.ambient_rms_gate == 800
    assert s.ambient_frame_rms_threshold == 500
    assert s.ambient_min_speech_frame_ratio == 0.15
    assert s.ambient_max_cps == 10.0
    assert s.ambient_min_stt_chars == 5


# ── case 1: 全静音 → 拒（rms_too_low）─────────────────────────────


def test_pre_gate_rejects_pure_silence() -> None:
    s = _new_thresholds()
    audio = _silence(6.0)
    decision = pre_stt_gate(
        audio,
        rms_gate=s.ambient_rms_gate,
        frame_rms_threshold=s.ambient_frame_rms_threshold,
        min_speech_frame_ratio=s.ambient_min_speech_frame_ratio,
    )
    assert decision.pass_ is False
    assert decision.reason == "rms_too_low"
    assert decision.rms == 0.0


# ── case 2: 底噪 RMS≈700 在旧基线 (600) 能过，新基线 (800) 拒 ─────


def test_pre_gate_rejects_low_noise_floor_under_new_rms_gate() -> None:
    """模拟空房间风扇底噪：整段 RMS≈700。

    旧 echo 基线 ambient_rms_gate=600 时会放行 → STT 在底噪上幻觉。
    新基线 800 把这条堵死。
    """
    s = _new_thresholds()
    audio = _noise(6.0, target_rms=700, seed=2)
    measured_rms = integer_rms(audio)
    # 容差：LCG 不是真随机，实测 RMS 大约就是 target 上下
    assert 650 < measured_rms < 750, f"fixture RMS off: {measured_rms}"

    decision = pre_stt_gate(
        audio,
        rms_gate=s.ambient_rms_gate,
        frame_rms_threshold=s.ambient_frame_rms_threshold,
        min_speech_frame_ratio=s.ambient_min_speech_frame_ratio,
    )
    assert decision.pass_ is False
    assert decision.reason == "rms_too_low"

    # 同样的音频在 echo 基线 (600) 下不会被 RMS gate 拒（用于断言"我们确实收紧了"）
    decision_old = pre_stt_gate(
        audio,
        rms_gate=600,
        frame_rms_threshold=s.ambient_frame_rms_threshold,
        min_speech_frame_ratio=0.05,
    )
    assert decision_old.reason != "rms_too_low", "fixture 不应该在 echo 基线被 RMS 拒"


# ── case 3: 6s 里只有 0.5s 偶发噪声（活跃帧比 ~0.08）→ 拒 ────────


def test_pre_gate_rejects_short_burst_in_long_silence() -> None:
    """6s buffer 里只有 0.5s 高响度噪声 → 活跃帧比例 ≈ 0.5/6 ≈ 8.3%。

    旧 echo 基线 0.05 时能过（0.083 > 0.05），但 STT 极易在剩余 5.5s 静默上幻觉
    出"嗯。" "ですね" 等。新基线 0.15 把这种 case 卡死。
    """
    s = _new_thresholds()
    # 高响度的爆破段（RMS 远超整段 gate，避免被 case 2 路径误判）
    burst = _sine(0.5, amplitude=8000)
    sil = _silence(5.5)
    audio = burst + sil

    # 帧活跃率应在 0.05 < ratio < 0.15 这一窗口里
    ratio = speech_frame_ratio(audio, frame_rms_threshold=s.ambient_frame_rms_threshold)
    assert 0.05 < ratio < 0.15, f"fixture frame ratio off: {ratio}"

    decision = pre_stt_gate(
        audio,
        rms_gate=s.ambient_rms_gate,
        frame_rms_threshold=s.ambient_frame_rms_threshold,
        min_speech_frame_ratio=s.ambient_min_speech_frame_ratio,
    )
    # 整段 RMS 可能因为 0.5s 高响度被拉到 800 之上；不强行约束 reason，
    # 但要求一定要被拒（要么 rms_too_low，要么 speech_ratio_too_low）。
    assert decision.pass_ is False
    assert decision.reason in {"speech_ratio_too_low", "rms_too_low"}


# ── case 4: 正常说话（持续 sin amp=3000）→ 过 ────────────────────


def test_pre_gate_accepts_normal_speech_like_signal() -> None:
    """持续 sin 波 amp=3000 → RMS ≈ 2121，每个 20ms 帧都活跃。

    用来确认收紧后正常说话仍然能过；不允许"宁可漏过"过头到把正常对话也拒掉。
    """
    s = _new_thresholds()
    audio = _sine(6.0, amplitude=3000, freq_hz=220.0)

    rms = integer_rms(audio)
    ratio = speech_frame_ratio(audio, frame_rms_threshold=s.ambient_frame_rms_threshold)
    assert rms > 2000, f"normal speech RMS too low: {rms}"
    assert ratio > 0.95, f"normal speech frame ratio too low: {ratio}"

    decision = pre_stt_gate(
        audio,
        rms_gate=s.ambient_rms_gate,
        frame_rms_threshold=s.ambient_frame_rms_threshold,
        min_speech_frame_ratio=s.ambient_min_speech_frame_ratio,
    )
    assert decision.pass_ is True
    assert decision.reason == "ok"


# ── case 5: STT 输出 "嗯。"（2 字）→ 拒（min_chars=5）────────────


def test_hallucination_rejects_text_below_min_chars() -> None:
    s = _new_thresholds()
    audio = _silence(2.0)  # duration 不影响 short-text 路径

    is_hallu, reason = is_likely_hallucination(
        "嗯。",
        audio,
        max_cps=s.ambient_max_cps,
        min_chars=s.ambient_min_stt_chars,
    )
    assert is_hallu is True
    assert "too_short" in reason

    # 同样 2 字在 echo 基线 (min_chars=4) 也被拒 → 验证新阈值不改这条路径的方向；
    # 但 4 字短语（如 "嗯嗯嗯嗯"）在旧 4 时会过，在新 5 时被拒：
    is_hallu_4chars, _ = is_likely_hallucination(
        "嗯嗯嗯嗯",
        audio,
        max_cps=s.ambient_max_cps,
        min_chars=s.ambient_min_stt_chars,
    )
    assert is_hallu_4chars is True, "min_chars 收紧到 5 后，4 字应当被拒"

    is_hallu_4chars_old, _ = is_likely_hallucination(
        "嗯嗯嗯嗯",
        audio,
        max_cps=12.0,
        min_chars=4,
    )
    assert is_hallu_4chars_old is False, "在 echo 基线下 4 字应当能过（对照）"


# ── case 6: 长重复低 cps 也应被 hallucination gate 拒 ──────────────


def test_hallucination_rejects_long_repetition_even_when_cps_low() -> None:
    """STT 输出 "嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯"（12 字），音频 6s → cps=2.0。

    事故回归（2026-06-03）：真实环境里 STT 低速输出大量 "对对对/哈哈哈/这个这个"，
    cps 不高但仍污染 meeting_segments 和 diarizer。文本层必须能挡住这种复读幻觉。
    """
    s = _new_thresholds()
    audio = _silence(6.0)  # 6s 整段，duration_s = 6.0
    text = "嗯" * 12  # cps = 12/6 = 2.0

    is_hallu, reason = is_likely_hallucination(
        text,
        audio,
        max_cps=s.ambient_max_cps,
        min_chars=s.ambient_min_stt_chars,
    )
    assert is_hallu is True
    assert reason in {"repeated_unit", "repeated_filler_char", "filler_dominant"}


def test_hallucination_rejects_repeated_common_bad_stt_outputs() -> None:
    """覆盖真实截图/DB 中出现的垃圾转写。"""
    s = _new_thresholds()
    audio = _silence(12.0)
    for text in [
        "对对对对对",
        "哈哈哈哈哈哈",
        "走走走走走走",
        "来来来来来",
        "这个这个这个这个这个这个",
    ]:
        is_hallu, reason = is_likely_hallucination(
            text,
            audio,
            max_cps=s.ambient_max_cps,
            min_chars=s.ambient_min_stt_chars,
        )
        assert is_hallu is True, f"{text!r} should be dropped, got {reason}"


def test_hallucination_keeps_normal_short_ack_with_content() -> None:
    """不要误伤正常短答：含具体内容时应保留。"""
    s = _new_thresholds()
    audio = _silence(3.0)
    is_hallu, reason = is_likely_hallucination(
        "对，这个方案可以",
        audio,
        max_cps=s.ambient_max_cps,
        min_chars=s.ambient_min_stt_chars,
    )
    assert is_hallu is False, reason


# ── case 7: 长文本 cps=11.0 在旧基线能过，新基线 (10) 拒 ───────────


def test_hallucination_rejects_long_text_with_cps_above_new_max() -> None:
    """4s 音频 + 44 字文本 → cps = 11.0。

    旧 echo 基线 max_cps=12 时不会拒（11 < 12），但实际是 ASR 卡复读输出。
    新基线 10 把这种 case 拒掉。
    """
    s = _new_thresholds()
    audio = _silence(4.0)
    text = "我" * 44  # cps = 44 / 4 = 11.0

    is_hallu_new, reason_new = is_likely_hallucination(
        text,
        audio,
        max_cps=s.ambient_max_cps,
        min_chars=s.ambient_min_stt_chars,
    )
    assert is_hallu_new is True
    assert reason_new in {"cps_too_high(11.0>10.0)", "repeated_unit", "repeated_filler_char"}

    # 现在复读门先于 cps 生效，避免低速/高速复读都漏过。
