"""声纹识别 adapter: SpeechBrain ECAPA-TDNN 192-dim。

设计（v4，PR phase4-diar-deep 引入「活跃说话人」时间窗 + 短段归并）：
- 每个 speaker_id 维护**一个 centroid embedding**（不再是 list/ring buffer）
- 命中匹配 → EMA 融合：`centroid = (1-α) * centroid + α * new`，α=0.1
- **两阶段匹配**：先查每 context（meeting_id 或 "_ambient"）的活跃说话人 list
  （时间窗口默认 60s，宽松阈值 0.35），命中即复用 + EMA；不命中再走全局
  `_profiles`（保留主阈值 0.55，跨会话仍稳健）；仍不命中 + voiced 够长才注册新人。
- **短段归并**：voiced 段 < 800ms 且 context 有 last_speaker → 跳过 embed，直接归到
  上一 speaker（避免噪声 / 短促语气词独立 embed → 不像任何人 → 新建）。
- **持久化**：每次注册/更新都把 centroid 写回 `speakers.embedding_blob`
- **启动 hydrate**：从 repo 读所有已知 centroids → `_profiles`，恢复 `_counter`
- speechbrain lazy load；CI 无 speechbrain 也能跑（_embed 单测用 mock）

v4 修法（用户痛点 2026-05-28：2h 内 40+ unique speaker_id，实际 3 人）：
  老路径：`_best_match()` 在 hydrate 出来的全集 `_profiles` 上匹配，stale 历史
  centroid（曾经 explosion 的产物）会跟新 embed 偶然 0.50-0.65 → 错认。同一人
  跟自己 centroid 的 cos 在真实噪音 6s chunk 上常落 0.40-0.55 → 错过。
  新路径：实时聚类——人正在说话的那一阵子，活跃 list 里只有 1-3 个 centroid，
  EMA 持续被刷新，宽松阈值 0.35 ≪ 不同人 cos 0.0-0.2 的天然鸿沟，几乎不会误判。
  60s 没说话 → 离开活跃 list，下次回来走 global 严判（命中老 ID 算复用）。

v3 历史（spk-2 / ARCH-AUDIT §4 root #5b）：
- `identify_segments(audio_bytes)` 按 VAD 切句，每段独立 embed + match + EMA
- v4 在 v3 切句基础上额外加：context 活跃 list 第一层匹配 + 短段归并。

修法对应根因（ARCH-AUDIT §4 + 2026-05-28 phase4-diar-deep）：
- #1 / #9 → 持久化 + hydrate 解决 _profiles 重启丢光、跨进程 counter 漂移
- #3 → 阈值 0.65 → 0.70 →（text-clarity）0.55 →（v4）保持 0.55 + 活跃层 0.35
- #5a → ring buffer + 单 vec → EMA centroid 融合
- #5b → 单 chunk 整段 embed → VAD 句级切片 + 逐段 embed（v3）
- #8 → 删 settings.diarizer_min_audio_bytes，硬编码 1.0s (32000 bytes) 跳过
- phase4-diar-deep #1 → 全局 stale centroid 污染 → 活跃 list 时间窗
- phase4-diar-deep #2 → 短段独立 embed 不稳 → < 800ms 归到 last_speaker

spike 数据（echo experiments/2026-05-25_m27-on-91-deploy/RESULTS.md §6.15-6.16）：
  ECAPA 6 人混合 DER=0.0%（与 GE2E DER=43.8% 对比）
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.adapters.audio_gate import split_into_voiced_segments
from app.config import Settings
from app.ports.repository import RepositoryPort

logger = logging.getLogger("echodesk.diarizer.ecapa")

# 1.0s @ 16k mono 16bit；短于此直接跳过 embedding（保护下限，echo 对齐）
_MIN_BYTES_FOR_EMBED = 32_000

# 没传 meeting_id 时的活跃 context key（所有非会议 ambient chunk 共享同一活跃池）
_AMBIENT_CONTEXT = "_ambient"

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


@dataclass(slots=True)
class _ActiveSpeaker:
    """活跃说话人时间窗口条目：context 内最近 N 秒内说过话的人。

    - centroid 跟 _profiles 共享一份引用（同一个 numpy ndarray）；EMA 更新时
      _profiles 拿到新 ndarray，这里也同步替换（不指向旧值）。
    - last_seen_at 用于按 active_window_s 过期剔除。
    """

    speaker_id: str
    centroid: Any
    last_seen_at: datetime


@dataclass(slots=True)
class _ContextState:
    """每个 context（meeting_id 或 ambient）的活跃说话人 + 上次分配 ID。"""

    active: list[_ActiveSpeaker] = field(default_factory=list)
    last_speaker: str | None = None


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
        # speaker_id → single centroid (numpy ndarray, L2-normalized)；全局长期注册表
        self._profiles: dict[str, Any] = {}
        self._counter = 0
        self._hydrated = False
        self._lock = asyncio.Lock()
        # 每 context（meeting_id 或 "_ambient"）的活跃说话人 + last_speaker，
        # 见模块 docstring v4。
        self._contexts: dict[str, _ContextState] = {}

    async def hydrate(self) -> None:
        """启动时从 repo 把所有已知 centroid 读回 `_profiles`，恢复 `_counter`。

        - 没接 repo → 标记 hydrated=True，直接返回
        - settings.diarizer_persist_speakers=False（默认，phase4-speaker-reset PR）
          → 跳过 hydrate；进程内 _profiles 从 0 开始，重启即清空（embedding 仅内存）
        - embedding_blob 为空的旧记录 → 跳过（保留 label，等下次说话时重新注册）
        - _counter 从所有 `speaker_N` 形态的 ID 提取，取 max(N)
        """
        if self._repo is None or not self._settings.diarizer_persist_speakers:
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

    # ── v4：活跃说话人时间窗口（每 context 独立） ──────────────────
    def _get_context(self, context_id: str) -> _ContextState:
        ctx = self._contexts.get(context_id)
        if ctx is None:
            ctx = _ContextState()
            self._contexts[context_id] = ctx
        return ctx

    def _purge_inactive(self, ctx: _ContextState, now: datetime) -> None:
        """剔除超过 `diarizer_active_window_s` 没说话的活跃条目。

        last_speaker 不在这里清空——它表达"这个 context 最近一次拍板的 speaker_id"，
        是短段归并的锚点；只要 context 还活着就保留（即使时间窗过期）。
        """
        window_s = self._settings.diarizer_active_window_s
        if window_s <= 0 or not ctx.active:
            return
        cutoff = now - timedelta(seconds=window_s)
        ctx.active = [a for a in ctx.active if a.last_seen_at >= cutoff]

    def _best_active_match(self, vec: Any, ctx: _ContextState) -> tuple[str | None, float]:
        best_id: str | None = None
        best_sim = -1.0
        for a in ctx.active:
            sim = self._cosine(vec, a.centroid)
            if sim > best_sim:
                best_sim = sim
                best_id = a.speaker_id
        return best_id, best_sim

    def _touch_active(self, ctx: _ContextState, sid: str, centroid: Any, now: datetime) -> None:
        """活跃 list 里更新 / 新增 sid。同一 sid 只保留一条，centroid 用最新的。"""
        for a in ctx.active:
            if a.speaker_id == sid:
                a.centroid = centroid
                a.last_seen_at = now
                return
        ctx.active.append(_ActiveSpeaker(sid, centroid, now))

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
        # phase4-speaker-reset：persist=False（默认）时不写 speakers 表（embedding 仅内存）。
        # _profiles 仍在内存里维护，所以 active list / 全局 _profiles 匹配照常工作；
        # 进程重启即清空，对齐"不跨会议长期记忆声纹"。
        if self._repo is None or not self._settings.diarizer_persist_speakers:
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
        context_id: str = _AMBIENT_CONTEXT,
        now: datetime | None = None,
    ) -> str | None:
        """单段 embed + match + EMA（已持 self._lock）。

        两阶段匹配（v4 phase4-diar-deep）：
          1. 先在 context 活跃 list 内（时间窗 = diarizer_active_window_s）用宽松阈值
             `diarizer_active_match_threshold` 匹配——同一人在短时间内的 embed 抖动
             基本都能命中。命中即 EMA 更新 + 刷新 last_seen_at。
          2. 不命中再走全局 `_profiles`（跨会话注册表），保留主阈值
             `diarizer_match_threshold`。命中既复用 ID，也把该 ID 注入活跃 list（人
             回来说话了）。
          3. 都不命中且 `voiced_active_s ≥ min_for_new` → 注册新人 + 注入活跃 list。
          4. 段太短（voiced_active_s < min_for_new）→ 用 outlier 阈值再尝试一次回退；
             仍不行返回 None（不污染、不爆 ID）。

        参数：
          - voiced_active_s：段内真实活跃语音秒数（duration × active_ratio），spk-3
            引入，比段总长更接近"人声时长"；老调用方未传时退化到 dur_sec。
          - context_id：meeting_id 或 "_ambient"；活跃 list 按此隔离。
          - now：用于活跃 list 时间窗过期；测试可注入固定时钟。
        """
        if now is None:
            now = datetime.now(UTC)
        ctx = self._get_context(context_id)
        self._purge_inactive(ctx, now)

        vec = await self._embed(audio_bytes, sample_rate)

        # ── 阶段 1：活跃 list 宽松匹配 ──
        active_id, active_sim = self._best_active_match(vec, ctx)
        active_threshold = self._settings.diarizer_active_match_threshold
        if active_id is not None and active_sim >= active_threshold:
            new_centroid = self._ema_update(active_id, vec)
            self._touch_active(ctx, active_id, new_centroid, now)
            ctx.last_speaker = active_id
            await self._persist(active_id, new_centroid)
            return active_id

        # ── 阶段 2：全局 _profiles 主阈值匹配 ──
        best_id, best_sim = self._best_match(vec)
        if best_id is not None and best_sim >= self._threshold:
            new_centroid = self._ema_update(best_id, vec)
            self._touch_active(ctx, best_id, new_centroid, now)
            ctx.last_speaker = best_id
            await self._persist(best_id, new_centroid)
            return best_id

        # ── 阶段 3/4：段长门控决定能否注册新人或回退 outlier ──
        active_s = voiced_active_s if voiced_active_s is not None else dur_sec
        min_for_new = self._settings.diarizer_min_voiced_seconds_for_new_profile
        outlier_threshold = self._settings.diarizer_outlier_match_threshold

        if active_s < min_for_new:
            # 段太短不允许注册新人；尝试 outlier 阈值兜底已知人
            if best_id is not None and best_sim >= outlier_threshold:
                new_centroid = self._ema_update(best_id, vec)
                self._touch_active(ctx, best_id, new_centroid, now)
                ctx.last_speaker = best_id
                await self._persist(best_id, new_centroid)
                return best_id
            logger.debug(
                "ecapa segment dropped: active_s=%.2f<%.2f, best_sim=%.3f<%.3f (active_sim=%.3f)",
                active_s,
                min_for_new,
                best_sim if best_id else 0.0,
                outlier_threshold,
                active_sim if active_id else 0.0,
            )
            return None

        # 用户 2026-05-28：ambient 长时间运行编号爆炸（11/19 个 ID 实际 3 人）。
        # ambient context 已达 max_speakers cap → 不再分配新 ID，强制复用 active
        # list 里 best_sim 最高的（如果没有 active 走 best_match；都没有再放行新建）。
        if context_id == _AMBIENT_CONTEXT:
            ambient_cap = getattr(self._settings, "diarizer_ambient_max_speakers", 0)
            if ambient_cap > 0 and len(ctx.active) >= ambient_cap:
                # 优先选 active list 内最像的；退而求其次选 global _profiles 最像的
                reuse_id = active_id if active_id is not None else best_id
                if reuse_id is not None:
                    new_centroid = self._ema_update(reuse_id, vec)
                    self._touch_active(ctx, reuse_id, new_centroid, now)
                    ctx.last_speaker = reuse_id
                    await self._persist(reuse_id, new_centroid)
                    logger.info(
                        "ecapa ambient cap hit (%d ≥ %d), reuse %s (active_sim=%.3f, best_sim=%.3f)",
                        len(ctx.active),
                        ambient_cap,
                        reuse_id,
                        active_sim if active_id else 0.0,
                        best_sim if best_id else 0.0,
                    )
                    return reuse_id

        # 注册新人：写 _profiles + 注入活跃 list
        self._counter += 1
        new_id = f"speaker_{self._counter}"
        self._profiles[new_id] = vec
        self._touch_active(ctx, new_id, vec, now)
        ctx.last_speaker = new_id
        await self._persist(new_id, vec)
        return new_id

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
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
        segs = await self.identify_segments(
            audio_bytes,
            sample_rate=sample_rate,
            meeting_id=meeting_id,
        )
        if segs:
            valid = [s for s in segs if s.speaker_id is not None]
            if not valid:
                return None
            valid.sort(key=lambda s: s.duration_ms, reverse=True)
            return valid[0].speaker_id

        # 切片切不出（但 buffer 足够长）→ 兜底走单段
        async with self._lock:
            dur = self._duration_sec(audio_bytes, sample_rate)
            return await self._identify_one(
                audio_bytes,
                sample_rate,
                dur,
                context_id=meeting_id or _AMBIENT_CONTEXT,
            )

    async def identify_segments(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> list[SegmentSpeaker]:
        """按 VAD 切句 → 每段独立 embed + match + EMA → 返回逐段结果。

        spk-2 修法核心：避免单 chunk 内多人混音被 embed 成混合向量。
        v4 phase4-diar-deep：voiced 段 < diarizer_short_segment_continuity_ms 且
        context 已有 last_speaker → 跳过 embed，直接归到 last_speaker（防短噪声
        段独立 embed → 不像任何人 → 新建路径）。
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

        context_id = meeting_id or _AMBIENT_CONTEXT
        short_seg_ms = self._settings.diarizer_short_segment_continuity_ms

        results: list[SegmentSpeaker] = []
        async with self._lock:
            ctx = self._get_context(context_id)
            now = datetime.now(UTC)
            for seg in voiced:
                seg_dur_sec = seg.duration_ms / 1000.0
                # spk-3 门控用 voiced active seconds，比段总长更接近"真实人声时长"。
                # active_ratio 由 audio_gate 帧扫描得到（每帧 RMS > threshold 算活跃）。
                voiced_active_s = seg_dur_sec * seg.active_ratio

                # v4 短段归并：voiced 段过短 + context 有 last_speaker → 不 embed，直接归并。
                # 仍刷新该 speaker 的 last_seen_at，让活跃 list 保持新鲜。
                if (
                    seg.duration_ms < short_seg_ms
                    and ctx.last_speaker is not None
                    and ctx.last_speaker in self._profiles
                ):
                    last_sid = ctx.last_speaker
                    self._touch_active(ctx, last_sid, self._profiles[last_sid], now)
                    results.append(SegmentSpeaker(seg.start_ms, seg.end_ms, last_sid))
                    continue

                # 段长不够稳定 embed（< 1s）→ 跳过，但保留 start/end 给调用方
                if len(seg.audio_bytes) < _MIN_BYTES_FOR_EMBED:
                    results.append(SegmentSpeaker(seg.start_ms, seg.end_ms, None))
                    continue
                try:
                    sid = await self._identify_one(
                        seg.audio_bytes,
                        sample_rate,
                        seg_dur_sec,
                        voiced_active_s=voiced_active_s,
                        context_id=context_id,
                        now=now,
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
        """清空内存 profile + counter + 活跃 list（不动 DB；测试与显式 demo reset 用）。"""
        async with self._lock:
            self._profiles.clear()
            self._counter = 0
            self._contexts.clear()


class NullDiarizer:
    """禁用声纹时的 noop 实现（diarizer_enabled=False 时用）。"""

    async def hydrate(self) -> None:
        return None

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> str | None:
        return None

    async def identify_segments(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
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
