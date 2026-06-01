"""并发压测：分别打 STT / TTS / yunwu LLM 三个真上游，给出每个服务的稳定并发。

直接用后端 adapter（与线上同一条代码路径），不经 FastAPI/DB，隔离出"云服务本身
能扛多少并发"。每个并发档位发 2×C 个请求、信号量限流到 C 个在飞，统计成功率与
p50/p95 时延，找到"成功率=100% 且 p95 不爆"的最高档位作为稳定并发建议。

用法：
    .venv/bin/python scripts/stress/concurrency_stress.py --service tts --levels 1,2,4,8,16
    .venv/bin/python scripts/stress/concurrency_stress.py --service stt --wav /tmp/echo_fixtures/plain_speech.wav
    .venv/bin/python scripts/stress/concurrency_stress.py --service llm --levels 1,2,4,8
    .venv/bin/python scripts/stress/concurrency_stress.py --service all
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

sys.path.insert(0, ".")

from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.stt import make_stt
from app.adapters.tts.qwen3_tts import Qwen3TTS
from app.config import get_settings
from app.schemas.llm import ChatMessage

CallFn = Callable[[], Awaitable[None]]


@dataclass
class CallResult:
    ok: bool
    latency_s: float
    err: str = ""


def _read_pcm(wav_path: str) -> tuple[bytes, int]:
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    return frames, sr


async def _timed(fn: CallFn) -> CallResult:
    t0 = time.monotonic()
    try:
        await fn()
        return CallResult(ok=True, latency_s=time.monotonic() - t0)
    except Exception as e:
        return CallResult(ok=False, latency_s=time.monotonic() - t0, err=f"{type(e).__name__}: {e}")


async def _run_level(make_call: Callable[[], CallFn], concurrency: int, total: int) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def _guarded() -> CallResult:
        async with sem:
            return await _timed(make_call())

    wall0 = time.monotonic()
    results = await asyncio.gather(*[_guarded() for _ in range(total)])
    wall = time.monotonic() - wall0

    oks = [r for r in results if r.ok]
    fails = [r for r in results if not r.ok]
    lats = sorted(r.latency_s for r in oks)
    p50 = statistics.median(lats) if lats else 0.0
    p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0.0
    tput = len(oks) / wall if wall > 0 else 0.0
    flag = "OK " if not fails else "FAIL"
    print(
        f"  C={concurrency:<3} n={total:<3} success={len(oks)}/{total} "
        f"p50={p50:6.2f}s p95={p95:6.2f}s tput={tput:5.2f}/s wall={wall:6.2f}s [{flag}]"
    )
    if fails:
        sample = fails[0].err[:160]
        print(f"      ↳ first error: {sample}")


async def stress_service(
    name: str, make_call: Callable[[], CallFn], levels: list[int], reps: int
) -> None:
    print(f"\n=== {name} 并发压测 ===")
    for c in levels:
        await _run_level(make_call, concurrency=c, total=c * reps)
        await asyncio.sleep(1.0)  # 档位间稍歇，避免上游瞬时排队叠加


def _tts_caller(settings: object) -> Callable[[], CallFn]:
    tts = Qwen3TTS(settings)  # type: ignore[arg-type]
    text = "这是一段用于并发压力测试的语音合成文本，长度适中。"

    def make() -> CallFn:
        async def call() -> None:
            res = await tts.synthesize_detailed(text)
            if not res.pcm:
                raise RuntimeError("empty pcm")
        return call

    return make


def _stt_caller(settings: object, wav_path: str) -> Callable[[], CallFn]:
    stt = make_stt(settings)  # type: ignore[arg-type]
    pcm, sr = _read_pcm(wav_path)

    def make() -> CallFn:
        async def call() -> None:
            segs = await stt.transcribe(pcm, sample_rate=sr)
            if not segs:
                raise RuntimeError("empty transcript")
        return call

    return make


def _llm_caller(settings: object) -> Callable[[], CallFn]:
    llm = OpenAICompatibleLLM(settings)  # type: ignore[arg-type]
    msgs = [
        ChatMessage(role="system", content="你是简洁的助理，一句话回答。"),
        ChatMessage(role="user", content="用一句话说明什么是检索增强生成。"),
    ]

    def make() -> CallFn:
        async def call() -> None:
            resp = await llm.chat(list(msgs), max_tokens=64, timeout_s=90)
            if not resp.content.strip():
                raise RuntimeError("empty content")
        return call

    return make


def _llm_fast_caller(settings: object) -> Callable[[], CallFn]:
    """快速通道 Qwen3-1.7B（意图分类 / ambient 加标点）。短输入短输出，贴近真实用途。"""
    llm = OpenAICompatibleLLM(settings)  # type: ignore[arg-type]
    model = settings.llm_fast_model  # type: ignore[attr-defined]
    msgs = [
        ChatMessage(role="system", content="给下面这句话加标点，只输出结果。"),
        ChatMessage(role="user", content="今天我们讨论一下第三季度的销售情况和市场推广策略"),
    ]

    def make() -> CallFn:
        async def call() -> None:
            resp = await llm.chat(list(msgs), model=model, max_tokens=64, timeout_s=60)
            if not resp.content.strip():
                raise RuntimeError("empty content")
        return call

    return make


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--service", choices=["stt", "tts", "llm", "llm_fast", "all"], default="all"
    )
    ap.add_argument("--levels", default="1,2,4,8,16")
    ap.add_argument("--llm-levels", default="1,2,4,8")
    ap.add_argument("--reps", type=int, default=2, help="每档发 reps×C 个请求")
    ap.add_argument("--wav", default="/tmp/echo_fixtures/plain_speech.wav")
    args = ap.parse_args()

    settings = get_settings()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    llm_levels = [int(x) for x in args.llm_levels.split(",") if x.strip()]

    if args.service in ("tts", "all"):
        await stress_service("TTS (tts.yoliyoli.uk qwen3_tts)", _tts_caller(settings), levels, args.reps)
    if args.service in ("stt", "all"):
        await stress_service(
            "STT (stt.yoliyoli.uk firered)", _stt_caller(settings, args.wav), levels, args.reps
        )
    if args.service in ("llm", "all"):
        await stress_service(
            "LLM (yunwu MiniMax-M2.7)", _llm_caller(settings), llm_levels, args.reps
        )
    if args.service in ("llm_fast", "all"):
        await stress_service(
            "LLM-fast (Qwen3-1.7B @ llm-fast.yoliyoli.uk)",
            _llm_fast_caller(settings),
            levels,
            args.reps,
        )


if __name__ == "__main__":
    asyncio.run(main())
