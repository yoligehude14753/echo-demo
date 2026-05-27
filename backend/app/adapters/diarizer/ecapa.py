"""声纹识别 adapter: SpeechBrain ECAPA-TDNN 192-dim。

设计（v3，PR echodesk-spk-2 引入 VAD 句级切片）：
- 每个 speaker_id 维护**一个 centroid embedding**（不再是 list/ring buffer）
- 命中匹配 → EMA 融合：`centroid = (1-α) * centroid + α * new`，α=0.1
- 阈值 0.70（之前 0.65 在 6s 含噪 chunk 上过严，触发 speaker explosion）
- **持久化**：每次注册/更新都把 centroid 写回 `speakers.embedding_blob`
- **启动 hydrate**：从 repo 读所有已知 centroids → `_profiles`，恢复 `_counter`
- speechbrain lazy load；CI 无 speechbrain 也能跑（_embed 单测用 mock）

v3 新增（spk-2 / ARCH-AUDIT §4 root #5b）：
- `identify_segments(audio_bytes)` 接收整段 6s ambient chunk，内部按 VAD 切成多个
  voiced 段，每段独立 embed + match + EMA。**这是 speaker explosion 的关键修法**：
  之前 6s chunk 里 A、B 交替说话时整段 embed 是混合向量，被判为新人；现在按句切。
- 原 `identify(audio_bytes)` 仍可用（向后兼容 + 单段路径），但 ambient 主链路统一走
  `identify_segments`；老接口转发到 segments 实现并聚合返回最长段的 speaker。

修法对应根因（ARCH-AUDIT §4）：
- #1 / #9 → 持久化 + hydrate 解决 _profiles 重启丢光、跨进程 counter 漂移
- #3 → 阈值 0.65 → 0.70
- #5a → ring buffer + 单 vec → EMA centroid 融合
- #5b → 单 chunk 整段 embed → VAD 句级切片 + 逐段 embed（v3）
- #8 → 删 settings.diarizer_min_audio_bytes，硬编码 1.0s (32000 bytes) 跳过

spike 数据（echo experiments/2026-05-25_m27-on-91-deploy/RESULTS.md §6.15-6.16）：
  ECAPA 6 人混合 DER=0.0%（与 GE2E DER=43.8% 对比）
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.adapters.audio_gate import split_into_voiced_segments
from app.config import Settings
from app.ports.repository import RepositoryPort

logger = logging.getLogger("echodesk.diarizer.ecapa")

# 1.0s @ 16k mono 16bit；短于此直接跳过 embedding（保护下限，echo 对齐）
_MIN_BYTES_FOR_EMBED = 32_000

# spk-3 把以下两个 magic number 改成 settings 可配（diarizer_min_voiced_seconds_for_new_profile /
# diarizer_outlier_match_threshold），便于 spk-5 真实多人音频回归调参。
# 真正的"段够不够长能注册新人"门控基于 voiced_active_s（duration × active_ratio）
# 而不是 audio_bytes 总长，因为 spk-2 切句后段长本身就是 voiced 区间，但偶有夹杂
# 短噪声不应虚增 active 计数。

_SPEAKER_ID_RE = re.compile(r"^speaker_(\d+)$")


@dataclass(slots=True, frozen=True)
class SegmentSpeaker:
    """`identify_segments` 单段结果。

    - speaker_id 为 None 表示该段太短/embed 失败，被跳过（不计入主导 speaker）
    """

    start_ms: int
    end_ms: int
    speaker_id: str | None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class DiarizerError(RuntimeError):
    pass


def _vec_to_blob(vec: Any) -> bytes:
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def _blob_to_vec(blob: bytes) -> Any:
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32)


class ECAPADiarizer:
    """实现 ports.diarizer.DiarizerPort。"""

    def __init__(
        self,
        settings: Settings,
        *,
        repository: RepositoryPort | None = None,
    ) -> None:
        self._settings = settings
        self._threshold = settings.diarizer_match_threshold
        self._alpha = settings.diarizer_centroid_ema_alpha
        self._enabled = settings.diarizer_enabled
        self._repo = repository
        self._encoder: Any = None
        # speaker_id → single centroid (numpy ndarray, L2-normalized)
        self._profiles: dict[str, Any] = {}
        self._counter = 0
        self._hydrated = False
        self._lock = asyncio.Lock()

    async def hydrate(self) -> None:
        """启动时从 repo 把所有已知 centroid 读回 `_profiles`，恢复 `_counter`。

        - 没接 repo → 标记 hydrated=True，直接返回
        - embedding_blob 为空的旧记录 → 跳过（保留 label，等下次说话时重新注册）
        - _counter 从所有 `speaker_N` 形态的 ID 提取，取 max(N)
        """
        if self._repo is None:
            self._hydrated = True
            return
        try:
            rows = await self._repo.list_speakers()
        except Exception as e:  # pragma: no cover
            logger.warning("ecapa hydrate list_speakers failed: %s", e)
            self._hydrated = True
            return

        loaded = 0
        max_n = 0
        async with self._lock:
            for r in rows:
                m = _SPEAKER_ID_RE.match(r.speaker_id)
                if m:
                    n = int(m.group(1))
                    max_n = max(max_n, n)
                if r.embedding_blob:
                    try:
                        vec = _blob_to_vec(r.embedding_blob)
                        if vec.size > 0:
                            self._profiles[r.speaker_id] = vec
                            loaded += 1
                    except Exception as e:  # pragma: no cover
                        logger.warning("ecapa hydrate decode failed for %s: %s", r.speaker_id, e)
            self._counter = max_n
            self._hydrated = True
        logger.info("ecapa hydrated: %d profiles loaded, counter=%d", loaded, self._counter)

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
        for sid, centroid in self._profiles.items():
            sim = self._cosine(vec, centroid)
            if sim > best_sim:
                best_sim = sim
                best_id = sid
        return best_id, best_sim

    @staticmethod
    def _duration_sec(audio_bytes: bytes, sample_rate: int) -> float:
        return len(audio_bytes) / (sample_rate * 2)

    def _ema_update(self, sid: str, vec: Any) -> Any:
        """EMA 融合 + 重新 L2 归一化。返回新 centroid。"""
        import numpy as np

        old = self._profiles.get(sid)
        if old is None:
            new = np.asarray(vec, dtype=np.float32)
        else:
            new = (1.0 - self._alpha) * old + self._alpha * vec
        norm = float(np.linalg.norm(new))
        new = new / (norm + 1e-8)
        self._profiles[sid] = new
        return new

    async def _persist(self, sid: str, vec: Any) -> None:
        if self._repo is None:
            return
        try:
            await self._repo.upsert_speaker(
                sid,
                captured_at=datetime.now(UTC),
                embedding_blob=_vec_to_blob(vec),
            )
        except Exception as e:  # pragma: no cover
            logger.warning("ecapa persist %s failed: %s", sid, e)

    async def _identify_one(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        dur_sec: float,
        *,
        voiced_active_s: float | None = None,
    ) -> str | None:
        """单段 embed + match + EMA（已持 self._lock）。

        提取自原 `identify` 主体，给 `identify_segments` 复用。

        spk-3 引入 voiced_active_s：段内真实活跃语音的秒数（duration × active_ratio）。
        - 若 voiced_active_s 不够长（< settings.diarizer_min_voiced_seconds_for_new_profile）
          → 不允许注册新人；尝试回退到最相似已知人（sim >= outlier_threshold 才命中）
        - 若 voiced_active_s 充足 → 正常路径（命中或注册新人）
        - 调用方未传 voiced_active_s 时退化为 dur_sec（向后兼容）
        """
        vec = await self._embed(audio_bytes, sample_rate)
        best_id, best_sim = self._best_match(vec)

        if best_id is not None and best_sim >= self._threshold:
            new_centroid = self._ema_update(best_id, vec)
            await self._persist(best_id, new_centroid)
            return best_id

        # 注册新人门控：用 voiced_active_s（更准）；老调用方退化到 dur_sec
        active_s = voiced_active_s if voiced_active_s is not None else dur_sec
        min_for_new = self._settings.diarizer_min_voiced_seconds_for_new_profile
        outlier_threshold = self._settings.diarizer_outlier_match_threshold

        if active_s < min_for_new:
            # 段太短不允许注册新人，尝试回退已知人
            if best_id is not None and best_sim >= outlier_threshold:
                new_centroid = self._ema_update(best_id, vec)
                await self._persist(best_id, new_centroid)
                return best_id
            # 既不够注册又找不到够相似的已知人 → 丢弃该段（返回 None）
            logger.debug(
                "ecapa segment dropped: active_s=%.2f<%.2f, best_sim=%.3f<%.3f",
                active_s,
                min_for_new,
                best_sim if best_id else 0.0,
                outlier_threshold,
            )
            return None

        self._counter += 1
        new_id = f"speaker_{self._counter}"
        self._profiles[new_id] = vec
        await self._persist(new_id, vec)
        return new_id

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        """单段 identify（向后兼容；ambient 主链路用 identify_segments）。

        改写为：内部走 identify_segments → 取最长段的 speaker_id。
        若整段没切出任何 voiced 段（噪声/静音），返回 None。

        对极短的 buffer 仍直接走老路（不切片），保留单段 ECAPA 行为，因为：
        - 调用方可能传 < min_segment_ms 的样本（测试 + 老代码）
        - 切了 → []，但单段 embed 其实是 fail-soft 的，按老逻辑跑能匹配上就行
        """
        if not self._enabled:
            return None
        if len(audio_bytes) < _MIN_BYTES_FOR_EMBED:
            return None

        # 走句级切片
        segs = await self.identify_segments(audio_bytes, sample_rate=sample_rate)
        if segs:
            valid = [s for s in segs if s.speaker_id is not None]
            if not valid:
                return None
            valid.sort(key=lambda s: s.duration_ms, reverse=True)
            return valid[0].speaker_id

        # 切片切不出（但 buffer 足够长）→ 兜底走单段
        async with self._lock:
            dur = self._duration_sec(audio_bytes, sample_rate)
            return await self._identify_one(audio_bytes, sample_rate, dur)

    async def identify_segments(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> list[SegmentSpeaker]:
        """按 VAD 切句 → 每段独立 embed + match + EMA → 返回逐段结果。

        spk-2 修法核心：避免单 chunk 内多人混音被 embed 成混合向量。
        """
        if not self._enabled:
            return []
        if len(audio_bytes) < _MIN_BYTES_FOR_EMBED:
            return []

        voiced = split_into_voiced_segments(
            audio_bytes,
            frame_rms_threshold=self._settings.ambient_frame_rms_threshold,
        )
        if not voiced:
            return []

        results: list[SegmentSpeaker] = []
        async with self._lock:
            for seg in voiced:
                # 段长不够稳定 embed（< 1s）→ 跳过，但保留 start/end 给调用方
                seg_dur_sec = seg.duration_ms / 1000.0
                # spk-3 门控用 voiced active seconds，比段总长更接近"真实人声时长"。
                # active_ratio 由 audio_gate 帧扫描得到（每帧 RMS > threshold 算活跃）。
                voiced_active_s = seg_dur_sec * seg.active_ratio
                if len(seg.audio_bytes) < _MIN_BYTES_FOR_EMBED:
                    results.append(SegmentSpeaker(seg.start_ms, seg.end_ms, None))
                    continue
                try:
                    sid = await self._identify_one(
                        seg.audio_bytes,
                        sample_rate,
                        seg_dur_sec,
                        voiced_active_s=voiced_active_s,
                    )
                except Exception as e:  # pragma: no cover
                    logger.warning(
                        "ecapa segment embed failed at [%d,%d]ms: %s",
                        seg.start_ms,
                        seg.end_ms,
                        e,
                    )
                    sid = None
                results.append(SegmentSpeaker(seg.start_ms, seg.end_ms, sid))
        return results

    async def reset(self) -> None:
        """清空内存 profile + counter（不动 DB；测试与显式 demo reset 用）。"""
        async with self._lock:
            self._profiles.clear()
            self._counter = 0


class NullDiarizer:
    """禁用声纹时的 noop 实现（diarizer_enabled=False 时用）。"""

    async def hydrate(self) -> None:
        return None

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        return None

    async def identify_segments(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> list[SegmentSpeaker]:
        return []

    async def reset(self) -> None:
        return None


def make_diarizer(
    settings: Settings,
    *,
    repository: RepositoryPort | None = None,
) -> ECAPADiarizer | NullDiarizer:
    if not settings.diarizer_enabled:
        return NullDiarizer()
    return ECAPADiarizer(settings, repository=repository)


__all__ = ["DiarizerError", "ECAPADiarizer", "NullDiarizer", "make_diarizer"]
