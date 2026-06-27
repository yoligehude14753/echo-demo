"""LLMPunctuator 单测：FireRedASR2 无 punc 选项 → 后处理补标点链路。

覆盖三大场景（对应 text-clarity PR 任务 Part A）：
1. happy：mock LLM 返回 well-formed JSON → segments 文本被加上标点
2. batch：多段一次性发，逐段返回
3. fail-soft：LLM timeout / parse 失败 / 校验不通过 → 退回原文本（不抛）
4. disabled：flag 关 → noop
5. safety guard：LLM 加字 / 删字 / 长度爆炸 → 校验拒绝，退回原文本
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.stt.llm_punctuator import LLMPunctuator, _is_safe_rewrite
from app.config import Settings
from app.schemas.llm import LLMResponse, LLMUsage
from app.schemas.meeting import TranscriptSegment


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ambient_llm_punctuate": True,
        "ambient_punctuator_timeout_s": 2.0,
        "llm_fast_model": "qwen3.5-9b-local-gpu0",
        "llm_fast_max_tokens": 512,
    }
    base.update(overrides)
    return Settings(**base)


def _mock_llm_with_json(content: str) -> MagicMock:
    """LLM mock：chat() 返回固定 JSON content。"""
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=LLMResponse(
            content=content,
            model="qwen3.5-9b-local-gpu0",
            finish_reason="stop",
            usage=LLMUsage(),
        )
    )
    return llm


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_happy_path_adds_punctuation() -> None:
    """单段 → LLM 返回带标点 → segment.text 被替换。"""
    raw = "我现在身份不是打字员我是代码总导演"
    punctuated = "我现在身份不是打字员，我是代码总导演。"
    llm = _mock_llm_with_json(f'{{"items": [{{"id": 0, "text": "{punctuated}"}}]}}')
    p = LLMPunctuator(llm, _settings())

    segs = [TranscriptSegment(text=raw, start_ms=0, end_ms=2000)]
    out = await p.punctuate(segs)

    assert len(out) == 1
    assert out[0].text == punctuated
    # 其它字段不动
    assert out[0].start_ms == 0
    assert out[0].end_ms == 2000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_batch_multiple_segments() -> None:
    """3 段一次性发，3 段一次性返回。验证批量减少 LLM 调用。"""
    seg_texts = [
        "每天准时下班体验vip沟通的绝对爽感",
        "点击视频下方右下角小黄车现在下单",
        "还送老韩的三天直播课手把手",
    ]
    new_texts = [
        "每天准时下班，体验vip沟通的绝对爽感。",
        "点击视频下方右下角小黄车，现在下单。",
        "还送老韩的三天直播课，手把手。",
    ]
    items_json = ", ".join(f'{{"id": {i}, "text": "{t}"}}' for i, t in enumerate(new_texts))
    llm = _mock_llm_with_json(f'{{"items": [{items_json}]}}')
    p = LLMPunctuator(llm, _settings())

    segs = [
        TranscriptSegment(text=t, start_ms=i * 1000, end_ms=(i + 1) * 1000)
        for i, t in enumerate(seg_texts)
    ]
    out = await p.punctuate(segs)

    assert [s.text for s in out] == new_texts
    # 关键：只调用 1 次 LLM（batch 全部 3 段）
    assert llm.chat.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_disabled_flag_is_noop() -> None:
    """AMBIENT_LLM_PUNCTUATE=false → 直接返回原 segments，不调 LLM。"""
    llm = _mock_llm_with_json('{"items": []}')
    p = LLMPunctuator(llm, _settings(ambient_llm_punctuate=False))

    segs = [TranscriptSegment(text="嗨", start_ms=0, end_ms=100)]
    out = await p.punctuate(segs)

    assert out == segs
    llm.chat.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_empty_segments_returns_empty() -> None:
    llm = _mock_llm_with_json('{"items": []}')
    p = LLMPunctuator(llm, _settings())
    out = await p.punctuate([])
    assert out == []
    llm.chat.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_llm_timeout_falls_back_to_raw() -> None:
    """LLM 调用超时 → 退回原 segments；不抛异常。"""
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=TimeoutError("simulated"))
    p = LLMPunctuator(llm, _settings(ambient_punctuator_timeout_s=0.5))

    raw = "测试原文"
    segs = [TranscriptSegment(text=raw, start_ms=0, end_ms=500)]
    out = await p.punctuate(segs)

    assert len(out) == 1
    assert out[0].text == raw  # 原样返回


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_llm_unparseable_returns_raw() -> None:
    """LLM 返回非 JSON → 校验失败退回原文本。"""
    llm = _mock_llm_with_json("not a json at all, sorry")
    p = LLMPunctuator(llm, _settings())

    segs = [TranscriptSegment(text="原始文本", start_ms=0, end_ms=500)]
    out = await p.punctuate(segs)
    assert out[0].text == "原始文本"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_llm_error_returns_raw() -> None:
    """LLM raise → 退回原 segments；不向上传播。"""
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("yunwu 500"))
    p = LLMPunctuator(llm, _settings())

    out = await p.punctuate([TranscriptSegment(text="哈哈", start_ms=0, end_ms=100)])
    assert out[0].text == "哈哈"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_rejects_when_llm_changes_characters() -> None:
    """LLM 把"打字员"改成"工程师"（加字/改字）→ safety guard 拒绝，退回原文本。"""
    raw = "我是打字员"
    bad = "我是工程师。"  # 加字 + 改字
    llm = _mock_llm_with_json(f'{{"items": [{{"id": 0, "text": "{bad}"}}]}}')
    p = LLMPunctuator(llm, _settings())

    out = await p.punctuate([TranscriptSegment(text=raw, start_ms=0, end_ms=500)])
    assert out[0].text == raw  # 改写不安全，退回


@pytest.mark.unit
@pytest.mark.asyncio
async def test_punctuate_partial_updates_preserve_others() -> None:
    """LLM 只返回部分 id（如丢了 id=1）→ 该段保留原文，其它段正常替换。"""
    raw_texts = ["原文一", "原文二", "原文三"]
    # 只返回 id 0 和 2
    items = '{"items": [{"id": 0, "text": "原文一。"},{"id": 2, "text": "原文三。"}]}'
    llm = _mock_llm_with_json(items)
    p = LLMPunctuator(llm, _settings())

    segs = [
        TranscriptSegment(text=t, start_ms=i * 100, end_ms=(i + 1) * 100)
        for i, t in enumerate(raw_texts)
    ]
    out = await p.punctuate(segs)
    assert out[0].text == "原文一。"
    assert out[1].text == "原文二"  # 没被覆盖
    assert out[2].text == "原文三。"


@pytest.mark.unit
def test_safe_rewrite_accepts_pure_punctuation_addition() -> None:
    """护栏单测：只加标点的改写应通过。"""
    assert _is_safe_rewrite(
        "我现在身份不是打字员我是代码总导演", "我现在身份不是打字员，我是代码总导演。"
    )


@pytest.mark.unit
def test_safe_rewrite_rejects_added_characters() -> None:
    assert not _is_safe_rewrite("我是打字员", "我是打字员，我还是工程师。")


@pytest.mark.unit
def test_safe_rewrite_rejects_dropped_characters() -> None:
    assert not _is_safe_rewrite("我现在身份不是打字员", "我是打字员")


@pytest.mark.unit
def test_safe_rewrite_rejects_emoji_or_invalid_chars() -> None:
    assert not _is_safe_rewrite("我很开心", "我很开心😀")


@pytest.mark.unit
def test_safe_rewrite_rejects_empty_rewrite() -> None:
    assert not _is_safe_rewrite("有内容", "")


@pytest.mark.unit
def test_safe_rewrite_allows_newline_for_paragraphs() -> None:
    """允许 LLM 用换行做自然分段。"""
    raw = "第一句第二句"
    new = "第一句。\n第二句。"
    assert _is_safe_rewrite(raw, new)
