"""用真实 TTS 服务合成语音并落成 WAV，供真链路 E2E / STT 压测复用。

为什么用真 TTS 造样本：
- STT (`/capture/chunk`) 有 VAD 门（RMS / 活跃帧率），必须喂"真有语音能量"的音频；
  纯静音 / 白噪过不了门，测不出 STT 真实表现。
- 用真 TTS 合成的人声 PCM 一定能过门，且文本可控（含唤醒词），方便验证语音指令链路。

用法：
    .venv/bin/python scripts/stress/make_speech_fixture.py \
        --text "嘿一口 生成一个关于人工智能的PPT" \
        --out /tmp/echo_fixture_wake.wav

PCM 规格：16kHz / 16-bit / mono（与 /tts/speak 返回、/capture/chunk 期望一致）。
"""

from __future__ import annotations

import argparse
import sys
import wave

import httpx

SAMPLE_RATE = 16_000


def synth_to_wav(text: str, out_path: str, base_url: str, voice: str | None) -> None:
    payload: dict[str, object] = {"text": text}
    if voice:
        payload["voice"] = voice
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{base_url}/tts/speak", json=payload)
    if resp.status_code != 200:
        print(f"[fixture] TTS 失败 {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        raise SystemExit(2)
    pcm = resp.content
    if not pcm:
        print("[fixture] TTS 返回空 PCM", file=sys.stderr)
        raise SystemExit(2)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    dur_s = len(pcm) / 2 / SAMPLE_RATE
    print(f"[fixture] wrote {out_path} pcm_bytes={len(pcm)} duration={dur_s:.2f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8769")
    ap.add_argument("--voice", default=None)
    args = ap.parse_args()
    synth_to_wav(args.text, args.out, args.base_url, args.voice)


if __name__ == "__main__":
    main()
