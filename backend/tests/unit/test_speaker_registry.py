"""SpeakerRegistry 单测：legacy（persist=True）跨会议持久化路径。

phase4-speaker-reset（2026-05-28）：默认 ``diarizer_persist_speakers=False``
之后，全局编号 + 跨重启持久化属于 legacy 路径，所有依赖该行为的测试必须显式
``settings=Settings(diarizer_persist_speakers=True)``，与 per-meeting 默认行为
解耦。新路径覆盖在 ``test_speaker_per_meeting_counter.py``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.use_cases.speaker_registry import SpeakerRegistry


def _legacy_settings() -> Settings:
    """显式开启跨会议持久化，走 legacy 路径。"""
    return Settings(diarizer_persist_speakers=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_label_for_none_returns_unknown() -> None:
    reg = SpeakerRegistry(None, settings=_legacy_settings())
    label = await reg.label_for(None, captured_at=datetime.now(UTC))
    assert label == "未识别"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_label_for_sequential_assignment_without_repo() -> None:
    reg = SpeakerRegistry(None, settings=_legacy_settings())
    now = datetime.now(UTC)
    a = await reg.label_for("spk_A", captured_at=now)
    b = await reg.label_for("spk_B", captured_at=now)
    a2 = await reg.label_for("spk_A", captured_at=now)
    assert a == "说话人1"
    assert b == "说话人2"
    assert a2 == "说话人1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_label_for_persists_to_repo(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        reg = SpeakerRegistry(repo, settings=_legacy_settings())
        now = datetime.now(UTC)
        await reg.label_for("spk_A", captured_at=now)
        await reg.label_for("spk_B", captured_at=now)

        rows = await repo.list_speakers()
        assert {r.speaker_id for r in rows} == {"spk_A", "spk_B"}
        labels = {r.speaker_id: r.label for r in rows}
        assert labels["spk_A"] == "说话人1"
        assert labels["spk_B"] == "说话人2"
        # n_samples 第一次 = 1
        assert {r.n_samples for r in rows} == {1}
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_label_for_increments_n_samples_on_revisit(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        reg = SpeakerRegistry(repo, settings=_legacy_settings())
        now = datetime.now(UTC)
        await reg.label_for("spk_A", captured_at=now)
        await reg.label_for("spk_A", captured_at=now)
        await reg.label_for("spk_A", captured_at=now)
        r = await repo.get_speaker("spk_A")
        assert r is not None
        assert r.n_samples == 3
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_recovers_labels_across_process(tmp_path: Path) -> None:
    db_path = tmp_path / "echo.db"

    repo1 = SQLiteRepository(db_path)
    await repo1.init()
    try:
        reg1 = SpeakerRegistry(repo1, settings=_legacy_settings())
        now = datetime.now(UTC)
        await reg1.label_for("spk_A", captured_at=now)
        await reg1.label_for("spk_B", captured_at=now)
        await reg1.rename("spk_A", "李雷")
    finally:
        await repo1.aclose()

    # 进程 2：hydrate 后看老用户用旧名（李雷），新用户接着编号 3
    repo2 = SQLiteRepository(db_path)
    await repo2.init()
    try:
        reg2 = SpeakerRegistry(repo2, settings=_legacy_settings())
        await reg2.hydrate()
        now = datetime.now(UTC)
        assert (await reg2.label_for("spk_A", captured_at=now)) == "李雷"
        assert (await reg2.label_for("spk_B", captured_at=now)) == "说话人2"
        assert (await reg2.label_for("spk_C", captured_at=now)) == "说话人3"
    finally:
        await repo2.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rename_overrides_label(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        reg = SpeakerRegistry(repo, settings=_legacy_settings())
        now = datetime.now(UTC)
        await reg.label_for("spk_A", captured_at=now)
        await reg.rename("spk_A", "韩梅梅")
        # Re-query label_for 应直接拿改名后的
        assert (await reg.label_for("spk_A", captured_at=now)) == "韩梅梅"
        # repo 也持久了
        r = await repo.get_speaker("spk_A")
        assert r is not None
        assert r.label == "韩梅梅"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_hydrate_call_still_picks_up_existing_numbering(tmp_path: Path) -> None:
    """即使忘了调 hydrate()，新 registry 第一次分配也不会从 1 重新开始（合并 DB 现状）。"""
    db_path = tmp_path / "echo.db"
    repo1 = SQLiteRepository(db_path)
    await repo1.init()
    try:
        reg1 = SpeakerRegistry(repo1, settings=_legacy_settings())
        now = datetime.now(UTC)
        for sid in ("spk_A", "spk_B", "spk_C"):
            await reg1.label_for(sid, captured_at=now)
    finally:
        await repo1.aclose()

    repo2 = SQLiteRepository(db_path)
    await repo2.init()
    try:
        reg2 = SpeakerRegistry(repo2, settings=_legacy_settings())  # 不调 hydrate
        now = datetime.now(UTC)
        # 新人 spk_D：分配前内部自动合并 DB → 给"说话人4"
        assert (await reg2.label_for("spk_D", captured_at=now)) == "说话人4"
    finally:
        await repo2.aclose()
