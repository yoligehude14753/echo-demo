from __future__ import annotations

import pytest
from app.adapters.repo.migrator import run_migrations
from app.runtime.capture_selection import CaptureSelectionStore


@pytest.mark.asyncio
async def test_single_allows_only_selected_device(tmp_path) -> None:
    db = tmp_path / "capture.db"
    assert not (await run_migrations(db)).errors
    store = CaptureSelectionStore(db)
    selected = await store.update(
        "t", "u", mode="single", selected_device_ids=["a"], expected_revision=0
    )
    assert selected.revision == 1
    assert selected.allows("a")
    assert not selected.allows("b")


@pytest.mark.asyncio
async def test_multi_allows_selected_devices_and_isolates_others(tmp_path) -> None:
    db = tmp_path / "capture.db"
    assert not (await run_migrations(db)).errors
    store = CaptureSelectionStore(db)
    selected = await store.update(
        "t", "u", mode="multi", selected_device_ids=["a", "b", "a"], expected_revision=0
    )
    assert selected.selected_device_ids == ("a", "b")
    assert selected.allows("a") and selected.allows("b")
    assert not selected.allows("c")


@pytest.mark.asyncio
async def test_revision_conflict_is_rejected(tmp_path) -> None:
    db = tmp_path / "capture.db"
    assert not (await run_migrations(db)).errors
    store = CaptureSelectionStore(db)
    await store.update("t", "u", mode="single", selected_device_ids=["a"], expected_revision=0)
    with pytest.raises(RuntimeError, match="revision conflict"):
        await store.update("t", "u", mode="single", selected_device_ids=["b"], expected_revision=0)


@pytest.mark.asyncio
async def test_single_requires_exactly_one_device(tmp_path) -> None:
    db = tmp_path / "capture.db"
    assert not (await run_migrations(db)).errors
    store = CaptureSelectionStore(db)
    with pytest.raises(ValueError, match="exactly one"):
        await store.update(
            "t", "u", mode="single", selected_device_ids=["a", "b"], expected_revision=0
        )


@pytest.mark.asyncio
async def test_selection_persists_across_store_instances(tmp_path) -> None:
    db = tmp_path / "capture.db"
    assert not (await run_migrations(db)).errors
    await CaptureSelectionStore(db).update(
        "t", "u", mode="multi", selected_device_ids=["a", "b"], expected_revision=0
    )
    restored = await CaptureSelectionStore(db).get("t", "u")
    assert restored.mode == "multi"
    assert restored.selected_device_ids == ("a", "b")
    assert restored.revision == 1
