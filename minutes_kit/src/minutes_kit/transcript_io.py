"""Transcript I/O：解析 .txt / .json / .jsonl 输入为 list[TranscriptTurn]。

支持的输入格式：

A) 行式文本（.txt）：
    [HH:MM:SS] 说话人A: 我们今天讨论 X
    [HH:MM:SS] 说话人B: 同意
    说话人C: 这一行可以没有时间戳

B) JSON 数组（.json）：
    [
      {"speaker": "A", "text": "...", "ts": "10:00:00"},
      {"speaker": "B", "text": "...", "ts": "2026-05-27T10:00:05+08:00"}
    ]

C) JSON Lines（.jsonl）：
    {"speaker": "A", "text": "..."}
    {"speaker": "B", "text": "..."}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

from minutes_kit.models import TranscriptTurn

_TS_PREFIX_RE = re.compile(
    r"^\s*\[?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]?\s+"
)

_SPEAKER_RE = re.compile(
    r"^(?P<speaker>[^:：\[\]\d][^:：]{0,39})\s*[:：]\s*(?P<text>.+?)\s*$"
)


def load_transcript(path: Path) -> list[TranscriptTurn]:
    """根据扩展名自动派发。"""
    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8")

    if suffix == ".json":
        return _parse_json(raw)
    if suffix == ".jsonl":
        return _parse_jsonl(raw)
    return _parse_text(raw)


def parse_transcript_text(raw: str) -> list[TranscriptTurn]:
    """直接解析文本字符串（demo server 用）。"""
    raw = raw.strip()
    if not raw:
        return []
    # 尝试 JSON 数组
    if raw.startswith("["):
        try:
            return _parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    # 尝试 JSON Lines
    if raw.startswith("{"):
        try:
            return _parse_jsonl(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return _parse_text(raw)


def _parse_json(raw: str) -> list[TranscriptTurn]:
    arr = json.loads(raw)
    if not isinstance(arr, list):
        raise ValueError("JSON 顶层不是数组")
    return [TranscriptTurn.from_dict(x) for x in arr if isinstance(x, dict) and x.get("text")]


def _parse_jsonl(raw: str) -> list[TranscriptTurn]:
    out: list[TranscriptTurn] = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(f"transcript JSONL 第 {i} 行解析失败，跳过: {exc}")
            continue
        if isinstance(obj, dict) and obj.get("text"):
            out.append(TranscriptTurn.from_dict(obj))
    return out


def _parse_text(raw: str) -> list[TranscriptTurn]:
    """行式纯文本。两阶段：先剥时间戳前缀，再 match speaker:text。

    支持的行格式（任一）：
        [HH:MM:SS] A: 你好
        [10:00] A: 你好
        A: 你好
        [00:00] 没有 speaker 的纯 ASR 文本（diarization 未跑）
        没时间戳也没 speaker 的纯文本句子
    """
    out: list[TranscriptTurn] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        ts = ""
        remaining = line
        ts_match = _TS_PREFIX_RE.match(line)
        if ts_match:
            ts = ts_match.group("ts").strip()
            remaining = line[ts_match.end():].strip()

        if not remaining:
            continue

        spk_match = _SPEAKER_RE.match(remaining)
        if spk_match:
            speaker = spk_match.group("speaker").strip() or "?"
            text = spk_match.group("text").strip()
            # speaker 太长（>20 字）很可能是误识别，把整段当 text
            if len(speaker) > 20:
                text = f"{speaker}: {text}"
                speaker = "?"
            if text:
                out.append(TranscriptTurn(speaker=speaker, text=text, ts=ts))
                continue

        # 走到这里：剥完时间戳后没有 speaker 标识，整段当 text
        if len(remaining) >= 2:
            out.append(TranscriptTurn(speaker="?", text=remaining, ts=ts))
        else:
            logger.debug(f"transcript 文本无法匹配格式，跳过: {line[:80]!r}")
    return out
