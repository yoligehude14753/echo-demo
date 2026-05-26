"""Diarizer adapter 单测：mock _embed 跳过 speechbrain。"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from app.adapters.diarizer import ECAPADiarizer, NullDiarizer, make_diarizer
from app.config import Settings


def _enabled_settings() -> Settings:
    return Settings(diarizer_enabled=True, diarizer_match_threshold=0.65)


def _disabled_settings() -> Settings:
    return Settings(diarizer_enabled=False)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_make_diarizer_returns_null_when_disabled() -> None:
    d = make_diarizer(_disabled_settings())
    assert isinstance(d, NullDiarizer)
    assert await d.identify(b"\x00" * 64_000) is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_make_diarizer_returns_ecapa_when_enabled() -> None:
    d = make_diarizer(_enabled_settings())
    assert isinstance(d, ECAPADiarizer)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_too_short_audio_returns_none() -> None:
    d = ECAPADiarizer(_enabled_settings())
    out = await d.identify(b"\x00" * 8)  # < 16000 bytes
    assert out is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_first_voice_registers_new_speaker() -> None:
    d = ECAPADiarizer(_enabled_settings())

    fake_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return fake_vec

    with patch.object(d, "_embed", side_effect=_fake_embed):
        # 5s @ 16k @ 16-bit = 160_000 bytes
        sid = await d.identify(b"\x00" * 160_000)
    assert sid == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_similar_voice_matches_existing_speaker() -> None:
    d = ECAPADiarizer(_enabled_settings())

    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.9, 0.1, 0.0], dtype=np.float32)
    vec_b = vec_b / float(np.linalg.norm(vec_b))

    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid1 = await d.identify(b"\x00" * 160_000)
        sid2 = await d.identify(b"\x00" * 160_000)
    assert sid1 == sid2 == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dissimilar_voice_registers_new_speaker() -> None:
    d = ECAPADiarizer(_enabled_settings())

    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid1 = await d.identify(b"\x00" * 160_000)  # 5s
        sid2 = await d.identify(b"\x00" * 160_000)  # 5s
    assert sid1 == "speaker_1"
    assert sid2 == "speaker_2"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_short_clip_forces_fallback_to_best_match() -> None:
    """3s 短片段（< 4s）不允许注册新人，强制回退到最相似已知人（除非 sim<0.30）。"""
    d = ECAPADiarizer(_enabled_settings())

    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.4, 0.3, 0.0], dtype=np.float32)  # cosine ~0.8
    vec_b = vec_b / float(np.linalg.norm(vec_b))
    feed = [vec_a, vec_b]

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return feed.pop(0)

    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid1 = await d.identify(b"\x00" * 160_000)  # 5s 注册
        sid2 = await d.identify(b"\x00" * 96_000)  # 3s 应回退
    assert sid1 == sid2 == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reset_clears_profiles() -> None:
    d = ECAPADiarizer(_enabled_settings())
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec_a

    with patch.object(d, "_embed", side_effect=_fake_embed):
        await d.identify(b"\x00" * 160_000)
        await d.reset()
        sid = await d.identify(b"\x00" * 160_000)
    # reset 后计数从 1 重新开始
    assert sid == "speaker_1"
