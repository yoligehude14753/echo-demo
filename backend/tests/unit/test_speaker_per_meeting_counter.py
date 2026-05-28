"""SpeakerRegistry per-meeting counter（phase4-speaker-reset 默认路径）单测。

用户痛点（截图复现 2026-05-28）：UI 显示「说话人 18 / 19 / 20 / 21」——
SpeakerRegistry 走全局 counter（N = repo.speakers 总数 + 1），一开会就接老编号。

修法验证：
- 同一 SpeakerRegistry instance 分别处理 2 个 meeting，每个 meeting 内见 3 个
  独立 speaker_id → 各自分配 1/2/3，互不影响
- meeting A 出现的 speaker_id 在 meeting B 不复用其编号（B 重新从 1 开始）
- 不写 ``speakers`` 表（embedding 仅内存里用，进程重启就没了）
- ``diarizer_persist_speakers=True`` 仍走老路径（向后兼容；详见
  ``test_speaker_registry.py``）
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.use_cases.speaker_registry import SpeakerRegistry


def _new_settings() -> Settings:
    """新默认：persist=False（per-meeting counter）。"""
    return Settings(diarizer_persist_speakers=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_meeting_counter_starts_from_one_in_each_meeting() -> None:
    """同一 registry 喂两个 meeting，各 3 个 speaker_id，各自得 1/2/3。"""
    reg = SpeakerRegistry(None, settings=_new_settings())
    now = datetime.now(UTC)

    # meeting A: 三个不同的 speaker_id
    a1 = await reg.label_for("spk_X", captured_at=now, meeting_id="m-A")
    a2 = await reg.label_for("spk_Y", captured_at=now, meeting_id="m-A")
    a3 = await reg.label_for("spk_Z", captured_at=now, meeting_id="m-A")
    assert a1 == "说话人1"
    assert a2 == "说话人2"
    assert a3 == "说话人3"

    # meeting B: 完全不同的 3 人
    b1 = await reg.label_for("spk_P", captured_at=now, meeting_id="m-B")
    b2 = await reg.label_for("spk_Q", captured_at=now, meeting_id="m-B")
    b3 = await reg.label_for("spk_R", captured_at=now, meeting_id="m-B")
    assert b1 == "说话人1"
    assert b2 == "说话人2"
    assert b3 == "说话人3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_speaker_id_gets_independent_label_across_meetings() -> None:
    """同一 speaker_id 在不同 meeting 里独立编号（不共享 label）。"""
    reg = SpeakerRegistry(None, settings=_new_settings())
    now = datetime.now(UTC)

    # meeting A: spk_X 先来 → 说话人1；spk_Y → 说话人2
    a_x = await reg.label_for("spk_X", captured_at=now, meeting_id="m-A")
    _ = await reg.label_for("spk_Y", captured_at=now, meeting_id="m-A")
    assert a_x == "说话人1"

    # meeting B: spk_Y 先来 → 说话人1；然后 spk_X → 说话人2
    # （即使是 A 里被叫 1 号的，B 里也得让位给后到的）
    b_y = await reg.label_for("spk_Y", captured_at=now, meeting_id="m-B")
    b_x = await reg.label_for("spk_X", captured_at=now, meeting_id="m-B")
    assert b_y == "说话人1"
    assert b_x == "说话人2"

    # meeting A 的映射不受 B 影响
    assert (await reg.label_for("spk_X", captured_at=now, meeting_id="m-A")) == "说话人1"
    assert (await reg.label_for("spk_Y", captured_at=now, meeting_id="m-A")) == "说话人2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeat_speaker_within_meeting_returns_same_label() -> None:
    """同 meeting 内同 speaker_id 多次 label_for → 稳定返回同一 label。"""
    reg = SpeakerRegistry(None, settings=_new_settings())
    now = datetime.now(UTC)

    first = await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
    second = await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
    third = await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
    assert first == second == third == "说话人1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambient_sentinel_when_meeting_id_omitted() -> None:
    """meeting_id=None → 走 ``__ambient__`` 池，独立编号。"""
    reg = SpeakerRegistry(None, settings=_new_settings())
    now = datetime.now(UTC)

    # ambient 池
    amb1 = await reg.label_for("spk_X", captured_at=now)
    amb2 = await reg.label_for("spk_Y", captured_at=now)
    assert amb1 == "说话人1"
    assert amb2 == "说话人2"

    # meeting 池跟 ambient 池隔离
    m1 = await reg.label_for("spk_X", captured_at=now, meeting_id="m-1")
    assert m1 == "说话人1"  # 在 m-1 里他还是第一号


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_meeting_does_not_write_to_speakers_table(tmp_path: Path) -> None:
    """persist=False：即使注入 repo 也不写 ``speakers`` 表（embedding 仅内存）。"""
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        reg = SpeakerRegistry(repo, settings=_new_settings())
        now = datetime.now(UTC)
        await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
        await reg.label_for("spk_B", captured_at=now, meeting_id="m-1")
        await reg.label_for("spk_A", captured_at=now, meeting_id="m-2")

        rows = await repo.list_speakers()
        assert rows == [], f"persist=False 不应写 speakers 表，实际写入 {rows}"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_meeting_hydrate_is_no_op(tmp_path: Path) -> None:
    """persist=False：hydrate 不读 repo，老数据不影响新进程的编号。"""
    db_path = tmp_path / "echo.db"

    # 进程 1：用 legacy 路径写入老数据，模拟"以前积累的 18 个 speaker"
    repo1 = SQLiteRepository(db_path)
    await repo1.init()
    try:
        legacy = SpeakerRegistry(repo1, settings=Settings(diarizer_persist_speakers=True))
        now = datetime.now(UTC)
        for i in range(18):
            await legacy.label_for(f"spk_old_{i}", captured_at=now)
        rows = await repo1.list_speakers()
        assert len(rows) == 18  # 老数据确实 18 行
    finally:
        await repo1.aclose()

    # 进程 2：用 persist=False 起 → hydrate no-op、新 meeting 仍从 1 开始
    repo2 = SQLiteRepository(db_path)
    await repo2.init()
    try:
        reg = SpeakerRegistry(repo2, settings=_new_settings())
        await reg.hydrate()
        now = datetime.now(UTC)
        first = await reg.label_for("spk_new", captured_at=now, meeting_id="m-fresh")
        assert first == "说话人1"
        # 老 speaker_id 在新 meeting 也是从头编号
        old_in_new = await reg.label_for("spk_old_0", captured_at=now, meeting_id="m-fresh")
        assert old_in_new == "说话人2"
    finally:
        await repo2.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_path_when_persist_true(tmp_path: Path) -> None:
    """向后兼容：persist=True 仍走 legacy 全局编号 + 写 repo。"""
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        reg = SpeakerRegistry(repo, settings=Settings(diarizer_persist_speakers=True))
        now = datetime.now(UTC)
        # 全局编号：跨 meeting 累加
        a = await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
        b = await reg.label_for("spk_B", captured_at=now, meeting_id="m-2")
        c = await reg.label_for("spk_C", captured_at=now)  # ambient
        assert a == "说话人1"
        assert b == "说话人2"
        assert c == "说话人3"

        rows = await repo.list_speakers()
        assert {r.speaker_id for r in rows} == {"spk_A", "spk_B", "spk_C"}
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rename_propagates_across_meetings_in_per_meeting_mode() -> None:
    """persist=False：rename 把 speaker_id 在所有出现过的 meeting 里改名（不写 repo）。"""
    reg = SpeakerRegistry(None, settings=_new_settings())
    now = datetime.now(UTC)

    # spk_A 先在 m-1 出现 → 说话人1；又在 m-2 出现 → 说话人1（独立计数）
    await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")
    await reg.label_for("spk_B", captured_at=now, meeting_id="m-1")
    await reg.label_for("spk_A", captured_at=now, meeting_id="m-2")

    # 用户在某处把 spk_A 改名成 "Alice"
    await reg.rename("spk_A", "Alice")

    # 两个 meeting 里查 spk_A 都拿 "Alice"
    assert (await reg.label_for("spk_A", captured_at=now, meeting_id="m-1")) == "Alice"
    assert (await reg.label_for("spk_A", captured_at=now, meeting_id="m-2")) == "Alice"
    # spk_B 在 m-1 仍是 "说话人2"，没被牵连
    assert (await reg.label_for("spk_B", captured_at=now, meeting_id="m-1")) == "说话人2"
