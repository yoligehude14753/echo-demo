"""音频工具单测。"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from app.adapters.audio import pcm_to_wav, wav_to_float_mono16k


@pytest.mark.unit
def test_pcm_to_wav_roundtrip() -> None:
    samples = np.array([0, 100, -100, 32000, -32000], dtype=np.int16)
    wav = pcm_to_wav(samples.tobytes(), sample_rate=16_000)

    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        assert wf.getnframes() == 5


@pytest.mark.unit
def test_wav_to_float_mono16k_handles_int16() -> None:
    samples = np.array([0, 16384, -16384], dtype=np.int16)
    wav = pcm_to_wav(samples.tobytes(), sample_rate=16_000)
    arr = wav_to_float_mono16k(wav)
    assert arr is not None
    assert arr.shape == (3,)
    assert arr.dtype == np.float32
    np.testing.assert_allclose(arr, [0.0, 0.5, -0.5], atol=1e-4)


@pytest.mark.unit
def test_wav_to_float_resamples_8khz_to_16khz() -> None:
    samples = np.zeros(100, dtype=np.int16)
    wav = pcm_to_wav(samples.tobytes(), sample_rate=8_000)
    arr = wav_to_float_mono16k(wav)
    assert arr is not None
    # 8k -> 16k: 大约 200 samples
    assert 180 <= len(arr) <= 220
