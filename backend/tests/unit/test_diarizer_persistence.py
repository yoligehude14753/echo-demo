"""ECAPA centroid 持久化 + 启动 hydrate 单测（修 ARCH-AUDIT §4 root #1 #9）。

链路：
1. instance1.identify() → 写 repo (centroid)
2. 新 instance2 + hydrate() → 读回内存
3. instance2.identify(同人) → 命中同一 speaker_id（不会重新分配）
4. instance2.identify(新人) → speaker_id 从 hydrated counter 继续递增

用 in-memory FakeRepo（不依赖 SQLite I/O，跑得快）。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest
from app.adapters.diarizer import ECAPADiarizer
from app.config import Settings
from app.ports.repository import SpeakerProfileRecord


class FakeRepo:
    """实现 RepositoryPort 中 ECAPA 用到的子集：upsert_speaker / list_speakers。"""

    def __init__(self) -> None:
        self._rows: dict[str, SpeakerProfileRecord] = {}

    async def upsert_speaker(
        self,
        speaker_id: str,
        *,
        captured_at: datetime,
        label: str | None = None,
        embedding_blob: bytes | None = None,
    ) -> None:
        prev = self._rows.get(speaker_id)
        if prev is None:
            self._rows[speaker_id] = SpeakerProfileRecord(
                speaker_id=speaker_id,
                label=label,
                n_samples=1,
                first_seen_at=captured_at,
                last_seen_at=captured_at,
                embedding_blob=embedding_blob,
            )
        else:
            new_blob = embedding_blob if embedding_blob is not None else prev.embedding_blob
            new_label = label if label is not None else prev.label
            self._rows[speaker_id] = prev.model_copy(
                update={
                    "n_samples": prev.n_samples + 1,
                    "last_seen_at": captured_at,
                    "label": new_label,
                    "embedding_blob": new_blob,
                }
            )

    async def list_speakers(self) -> list[SpeakerProfileRecord]:
        return list(self._rows.values())


def _settings() -> Settings:
    # phase4-speaker-reset：本文件覆盖 ECAPA centroid 跨进程持久化（hydrate +
    # _persist），属于 legacy 路径 → 显式 diarizer_persist_speakers=True。
    # 默认 False 时 hydrate / _persist 都是 no-op，相关 assert 必然失败。
    return Settings(
        diarizer_enabled=True,
        diarizer_match_threshold=0.65,
        diarizer_persist_speakers=True,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_centroid_persisted_on_register() -> None:
    repo = FakeRepo()
    d = ECAPADiarizer(_settings(), repository=repo)
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def _fake_embed(_b: bytes, _sr: int) -> object:
        return vec

    with patch.object(d, "_embed", side_effect=_fake_embed):
        sid = await d.identify(b"\x00" * 160_000)

    assert sid == "speaker_1"
    rows = await repo.list_speakers()
    assert len(rows) == 1
    assert rows[0].speaker_id == "speaker_1"
    assert rows[0].embedding_blob is not None
    decoded = np.frombuffer(rows[0].embedding_blob, dtype=np.float32)
    assert np.allclose(decoded, vec)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hydrate_restores_profiles_and_counter() -> None:
    """关键 PR 目标：重启进程 → hydrate → 同人识别仍归同一 ID。"""
    repo = FakeRepo()
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    vec_c = np.array([0.0, 0.0, 1.0], dtype=np.float32)

    # session 1：注册三个人
    d1 = ECAPADiarizer(_settings(), repository=repo)
    feed1 = [vec_a, vec_b, vec_c]

    async def _embed1(_b: bytes, _sr: int) -> object:
        return feed1.pop(0)

    with patch.object(d1, "_embed", side_effect=_embed1):
        await d1.identify(b"\x00" * 160_000)
        await d1.identify(b"\x00" * 160_000)
        await d1.identify(b"\x00" * 160_000)
    assert d1._counter == 3
    assert set(d1._profiles.keys()) == {"speaker_1", "speaker_2", "speaker_3"}

    # session 2：新实例 hydrate
    d2 = ECAPADiarizer(_settings(), repository=repo)
    await d2.hydrate()

    assert d2._counter == 3, "counter 必须从 max(speaker_N) = 3 恢复"
    assert set(d2._profiles.keys()) == {"speaker_1", "speaker_2", "speaker_3"}
    for sid, vec in [("speaker_1", vec_a), ("speaker_2", vec_b), ("speaker_3", vec_c)]:
        assert np.allclose(d2._profiles[sid], vec), f"{sid} centroid 与原值不符"

    # 同人识别：vec_a 应仍归 speaker_1（不是创建 speaker_4）
    async def _embed2(_b: bytes, _sr: int) -> object:
        return vec_a

    with patch.object(d2, "_embed", side_effect=_embed2):
        sid = await d2.identify(b"\x00" * 160_000)
    assert sid == "speaker_1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hydrate_new_speaker_continues_counter() -> None:
    """hydrate 后新人 ID 应该从 max+1 继续，不会撞已有 ID。"""
    repo = FakeRepo()
    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_dissimilar = np.array([0.0, 0.0, 1.0], dtype=np.float32)

    async def _embed_a(_b: bytes, _sr: int) -> object:
        return vec_a

    d1 = ECAPADiarizer(_settings(), repository=repo)
    with patch.object(d1, "_embed", side_effect=_embed_a):
        await d1.identify(b"\x00" * 160_000)
    assert d1._counter == 1

    d2 = ECAPADiarizer(_settings(), repository=repo)
    await d2.hydrate()
    assert d2._counter == 1

    async def _embed(_b: bytes, _sr: int) -> object:
        return vec_dissimilar

    with patch.object(d2, "_embed", side_effect=_embed):
        sid = await d2.identify(b"\x00" * 160_000)

    assert sid == "speaker_2", "新人 ID 应从 hydrated counter+1 继续"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hydrate_without_repo_is_noop() -> None:
    """没注 repo 的实例 hydrate 应该静默成功（向后兼容）。"""
    d = ECAPADiarizer(_settings(), repository=None)
    await d.hydrate()
    assert d._counter == 0
    assert d._profiles == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hydrate_skips_empty_blob_rows() -> None:
    """旧数据 embedding_blob=NULL 不应炸；label 也不需要必填。"""
    repo = FakeRepo()
    # 直接造一条没有 blob 的老记录（模拟 PR 前数据）
    repo._rows["speaker_42"] = SpeakerProfileRecord(
        speaker_id="speaker_42",
        label="说话人42",
        n_samples=5,
        first_seen_at=datetime.fromtimestamp(0),
        last_seen_at=datetime.fromtimestamp(0),
        embedding_blob=None,
    )
    d = ECAPADiarizer(_settings(), repository=repo)
    await d.hydrate()

    # 没有 blob 的记录跳过加载，但 counter 仍恢复
    assert "speaker_42" not in d._profiles
    assert d._counter == 42
