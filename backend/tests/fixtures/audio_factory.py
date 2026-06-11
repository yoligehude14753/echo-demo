"""真音频 fixture：用 faster-qwen3-tts 合成两段中文 → 拼接 → 缓存到 ~/.echodesk/test-audio/。

测试流程：
1. fixture 已 cached → 直接读取
2. TTS 可达（heyi-bj :8094 = faster-qwen3-tts）→ 合成 + 写文件
3. 都不行 → return None → 测试 skip

为什么不入 git：
- audio binary 大（30s ~ 1MB, 2min ~ 4MB）
- 中文 + TTS voice 不可逆，binary diff 噪音大
"""

from __future__ import annotations

import asyncio
import socket
import struct
import wave
from pathlib import Path
from typing import Final

CACHE_DIR: Final[Path] = Path("~/.echodesk/test-audio").expanduser()
SAMPLE_RATE: Final[int] = 16_000

# 两个角色的对话脚本（设计成业务讨论，便于 LLM minutes 生成有意义内容）
SCRIPT_SHORT: Final[list[tuple[str, str]]] = [
    ("A", "今天我们讨论一下 Q3 销售目标的拆解，主要是华南区。"),
    ("B", "好的。华南这边主要是广东和深圳两个市场。"),
    ("A", "广东数据有些异常，actual 比 forecast 高出百分之二十，要不要核查一下？"),
    ("B", "我下周一给老张电话对一下渠道压货的情况。"),
]

SCRIPT_LONG: Final[list[tuple[str, str]]] = [
    ("A", "今天 Q3 复盘会议正式开始，我们先看销售数据。"),
    ("B", "好的。华南区营收 1200 万，环比增长 18%。"),
    ("A", "这个数据看起来很好，但是和我们的 forecast 偏离了多少？"),
    ("B", "actual 比 forecast 高出大概 20%，主要是广东渠道压货。"),
    ("A", "压货的话客户实际消化能力怎么样？需要警惕回款风险吗？"),
    ("B", "我建议下周一给老张打电话对齐一下渠道库存周期。"),
    ("A", "好的。除了华南区，华东那边的情况呢？"),
    ("B", "华东保持平稳，月均 800 万，主要客户结构没大变化。"),
    ("A", "那我们的 Q4 策略主要还是把华南消化掉的库存补上。"),
    ("B", "对，同时启动华北的市场预热，明年 Q1 是关键期。"),
]


def _audio_cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.wav"


def _can_connect(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _pcm16_to_wav_bytes(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _silence_pcm16(seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    n = int(seconds * sample_rate)
    return struct.pack(f"<{n}h", *([0] * n))


async def _synthesize_line(text: str) -> bytes:
    """调 faster-qwen3-tts 合成一句话，返回 PCM16 mono 16k。"""
    from app.adapters.tts import Qwen3TTS
    from app.config import Settings

    tts = Qwen3TTS(Settings(), timeout_s=30.0)
    return await tts.synthesize(text, sample_rate=SAMPLE_RATE)


async def _build_audio(script: list[tuple[str, str]]) -> bytes:
    """拼接脚本里所有句子（每句之间空 0.3s）+ 在每个 speaker 后做 0.6s 间隔。"""
    chunks: list[bytes] = []
    last_speaker: str | None = None
    for speaker, line in script:
        if last_speaker is not None and speaker != last_speaker:
            chunks.append(_silence_pcm16(0.6))
        chunks.append(await _synthesize_line(line))
        chunks.append(_silence_pcm16(0.3))
        last_speaker = speaker
    pcm = b"".join(chunks)
    return _pcm16_to_wav_bytes(pcm)


async def get_audio_fixture(kind: str = "short") -> bytes | None:
    """获取真音频 wav bytes。返回 None 表示不可用（测试 skip）。"""
    name = "real_chat_30s" if kind == "short" else "real_chat_120s"
    cache = _audio_cache_path(name)
    if cache.exists() and cache.stat().st_size > 30_000:
        return cache.read_bytes()

    if not _can_connect("localhost", 8094):
        return None

    script = SCRIPT_SHORT if kind == "short" else SCRIPT_LONG
    try:
        wav = await _build_audio(script)
    except Exception:
        return None

    if len(wav) < 30_000:
        return None
    cache.write_bytes(wav)
    return wav


def get_audio_fixture_sync(kind: str = "short") -> bytes | None:
    """同步包装（pytest fixture 用）。"""
    try:
        return asyncio.run(get_audio_fixture(kind))
    except RuntimeError:
        # 已在事件循环里，用新 loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(get_audio_fixture(kind))
        finally:
            loop.close()
