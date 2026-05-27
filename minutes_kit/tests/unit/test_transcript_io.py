"""transcript_io 解析测试。"""
from __future__ import annotations

import json
from pathlib import Path

from minutes_kit.transcript_io import (
    load_transcript,
    parse_transcript_text,
)


def test_parse_text_with_speaker_and_ts():
    raw = (
        "[10:00:00] A: 我们今天讨论\n"
        "[10:00:10] B: 同意\n"
        "C: 这一行没时间戳\n"
    )
    turns = parse_transcript_text(raw)
    assert len(turns) == 3
    assert turns[0].speaker == "A" and turns[0].ts == "10:00:00"
    assert turns[1].speaker == "B"
    assert turns[2].speaker == "C" and turns[2].ts == ""


def test_parse_text_handles_chinese_colon():
    raw = "[10:00] 张三：你好世界\n"
    turns = parse_transcript_text(raw)
    assert len(turns) == 1
    assert turns[0].speaker == "张三"
    assert turns[0].text == "你好世界"


def test_parse_text_no_speaker_diarization_fallback():
    """echo 真实 ASR 输出场景：只有时间戳没有 speaker。"""
    raw = (
        "[00:00] 这一块儿什么都能做所以他们有需求的话可以再来找我们\n"
        "[00:30] 一个一个表格那个表格就是比如说\n"
    )
    turns = parse_transcript_text(raw)
    assert len(turns) == 2
    assert all(t.speaker == "?" for t in turns)
    assert turns[0].ts == "00:00"
    assert "这一块儿" in turns[0].text


def test_parse_text_skips_comments_and_blank():
    raw = "# 这是注释\n\n  \nA: 真实内容\n"
    turns = parse_transcript_text(raw)
    assert len(turns) == 1
    assert turns[0].text == "真实内容"


def test_parse_text_long_pseudo_speaker_merged_back():
    """speaker > 20 字几乎肯定是误判，应该把整行当文本。"""
    raw = "[10:00] 这是一个非常长非常长的所谓 speaker 名字超过二十字了: 后面是文本\n"
    turns = parse_transcript_text(raw)
    assert len(turns) == 1
    assert turns[0].speaker == "?"
    assert "非常长" in turns[0].text


def test_parse_json_array():
    raw = json.dumps([
        {"speaker": "A", "text": "hello", "ts": "10:00:00"},
        {"speaker": "B", "text": "world"},
        {"speaker": "C", "text": "", "ts": "ignored"},  # 空 text 应被过滤
    ])
    turns = parse_transcript_text(raw)
    assert len(turns) == 2
    assert turns[0].speaker == "A"
    assert turns[1].text == "world"


def test_parse_jsonl():
    raw = (
        '{"speaker": "A", "text": "hello"}\n'
        'invalid json line\n'
        '{"speaker": "B", "text": "world"}\n'
    )
    turns = parse_transcript_text(raw)
    assert len(turns) == 2
    assert turns[1].speaker == "B"


def test_load_transcript_dispatches_by_extension(tmp_path: Path):
    txt = tmp_path / "x.txt"
    txt.write_text("A: hello\n", encoding="utf-8")
    j = tmp_path / "x.json"
    j.write_text('[{"speaker": "B", "text": "via json"}]', encoding="utf-8")
    jl = tmp_path / "x.jsonl"
    jl.write_text('{"speaker": "C", "text": "via jsonl"}\n', encoding="utf-8")

    assert load_transcript(txt)[0].speaker == "A"
    assert load_transcript(j)[0].speaker == "B"
    assert load_transcript(jl)[0].speaker == "C"


def test_empty_input_returns_empty_list():
    assert parse_transcript_text("") == []
    assert parse_transcript_text("   \n\n  ") == []
