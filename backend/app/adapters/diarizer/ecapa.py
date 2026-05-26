"""声纹识别 adapter: SpeechBrain ECAPA-TDNN 192-dim。

参考 echo backend/app/speaker/diarizer.py 的 ECAPA 实现，简化为：
- 内存中维护 speaker_id → centroid embedding
- 余弦相似度 > threshold(0.65) 视为同一人，否则注册新 speaker
- 短片段（< 4s）抗短碎片污染：不允许注册新人（除非 sim < 0.30 明显异类）
- 首次 import 时 lazy load speechbrain；CI 不装也能跑（adapter 单测用 mock）

spike 实测（echo experiments/2026-05-25_m27-on-91-deploy/RESULTS.md §6.15-6.16）：
  ECAPA 6 人混合 DER=0.0%（GE2E DER=43.8%）
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings

_MIN_DUR_FOR_NEW_PROFILE = 4.0
_OUTLIER_SIM_ALLOW_NEW = 0.30


class DiarizerError(RuntimeError):
    pass


class ECAPADiarizer:
    """实现 ports.diarizer.DiarizerPort。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._threshold = settings.diarizer_match_threshold
        self._min_audio_bytes = settings.diarizer_min_audio_bytes
        self._enabled = settings.diarizer_enabled
        self._encoder: Any = None
        self._profiles: dict[str, list[Any]] = {}  # speaker_id → list[ndarray]
        self._counter = 0
        self._lock = asyncio.Lock()

    async def _ensure_encoder(self) -> None:
        if not self._enabled:
            raise DiarizerError("diarizer disabled in settings")
        if self._encoder is not None:
            return
        try:
            from speechbrain.inference.speaker import SpeakerRecognition
        except ImportError as e:  # pragma: no cover
            raise DiarizerError(
                "speechbrain not installed; pip install speechbrain torch torchaudio"
            ) from e

        def _load() -> Any:
            return SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=".cache/speechbrain/ecapa",
                run_opts={"device": "cpu"},
            )

        self._encoder = await asyncio.to_thread(_load)

    async def _embed(self, audio_bytes: bytes, sample_rate: int) -> Any:
        await self._ensure_encoder()
        from app.adapters.audio import pcm_to_wav, wav_to_float_mono16k

        wav = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        arr = wav_to_float_mono16k(wav)
        if arr is None or len(arr) == 0:
            raise DiarizerError("audio empty after decode")

        def _run() -> Any:
            import numpy as np
            import torch

            tensor = torch.from_numpy(arr).unsqueeze(0)  # (1, T)
            emb = self._encoder.encode_batch(tensor)
            vec = emb.squeeze(0).squeeze(0).cpu().numpy()
            norm = float(np.linalg.norm(vec))
            return vec / (norm + 1e-8)

        return await asyncio.to_thread(_run)

    @staticmethod
    def _cosine(a: Any, b: Any) -> float:
        import numpy as np

        return float(np.dot(a, b))

    def _best_match(self, vec: Any) -> tuple[str | None, float]:
        best_id: str | None = None
        best_sim = -1.0
        for sid, embeddings in self._profiles.items():
            for e in embeddings:
                sim = self._cosine(vec, e)
                if sim > best_sim:
                    best_sim = sim
                    best_id = sid
        return best_id, best_sim

    @staticmethod
    def _duration_sec(audio_bytes: bytes, sample_rate: int) -> float:
        return len(audio_bytes) / (sample_rate * 2)

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        if not self._enabled:
            return None
        if len(audio_bytes) < self._min_audio_bytes:
            return None

        async with self._lock:
            vec = await self._embed(audio_bytes, sample_rate)
            best_id, best_sim = self._best_match(vec)
            dur = self._duration_sec(audio_bytes, sample_rate)

            if best_id is not None and best_sim >= self._threshold:
                self._profiles[best_id].append(vec)
                # 限制 ring buffer 大小，避免内存爆掉
                if len(self._profiles[best_id]) > 8:
                    self._profiles[best_id].pop(0)
                return best_id

            # 短片段不准注册新人，强制回退到最相似的现有人
            if (
                dur < _MIN_DUR_FOR_NEW_PROFILE
                and best_id is not None
                and best_sim >= _OUTLIER_SIM_ALLOW_NEW
            ):
                return best_id

            self._counter += 1
            new_id = f"speaker_{self._counter}"
            self._profiles[new_id] = [vec]
            return new_id

    async def reset(self) -> None:
        async with self._lock:
            self._profiles.clear()
            self._counter = 0


class NullDiarizer:
    """禁用声纹时的 noop 实现（diarizer_enabled=False 时用）。"""

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        return None

    async def reset(self) -> None:
        return None


def make_diarizer(settings: Settings) -> ECAPADiarizer | NullDiarizer:
    if not settings.diarizer_enabled:
        return NullDiarizer()
    return ECAPADiarizer(settings)


__all__ = ["DiarizerError", "ECAPADiarizer", "NullDiarizer", "make_diarizer"]
