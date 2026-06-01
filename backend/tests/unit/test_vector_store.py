"""VectorStore (hnswlib) 单测。

覆盖：
- add → search → delete → reload from disk 四个核心 case
- 替换同名 chunk_id（先 mark_deleted 旧 label）
- dim 不一致重启抛错
- mark_deleted 累积超阈值后 compact（live items 保留、deleted 清空）
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from app.adapters.rag.vector_store import VectorStore, VectorStoreError
from app.config import Settings


def _unit_vec(seed: int, dim: int) -> list[float]:
    """生成确定的单位向量（cosine space 友好）；不同 seed → 互不平行。"""
    rng = [(seed * 13 + i * 7 + 1) % 97 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in rng))
    return [x / norm for x in rng]


def _make_store(tmp_path: Path, *, dim: int = 8) -> VectorStore:
    s = Settings(rag_index_dir=tmp_path / "rag_index")
    return VectorStore(s, dim=dim, index_dir=tmp_path / "vec")


@pytest.mark.unit
async def test_add_search_delete_reload(tmp_path: Path) -> None:
    dim = 8
    store = _make_store(tmp_path, dim=dim)

    await store.add_batch(
        [
            ("doc1-c0", "doc1", _unit_vec(1, dim)),
            ("doc1-c1", "doc1", _unit_vec(2, dim)),
            ("doc2-c0", "doc2", _unit_vec(3, dim)),
        ]
    )

    assert await store.count() == 3
    assert await store.has_chunk("doc1-c0")
    assert not await store.has_chunk("missing")

    # search 用 doc1-c0 自身向量 → 自己应排第一
    hits = await store.search(_unit_vec(1, dim), top_k=3)
    assert len(hits) == 3
    assert hits[0][0] == "doc1-c0"
    assert hits[0][1] > 0.99  # cosine similarity ≈ 1

    # delete doc1 → 余下 doc2 一个
    n = await store.delete_doc("doc1")
    assert n == 2
    assert await store.count() == 1
    assert not await store.has_chunk("doc1-c0")

    # reload from disk：新建一个实例从 sidecar 恢复
    store2 = _make_store(tmp_path, dim=dim)
    assert await store2.count() == 1
    hits2 = await store2.search(_unit_vec(3, dim), top_k=1)
    assert len(hits2) == 1
    assert hits2[0][0] == "doc2-c0"


@pytest.mark.unit
async def test_replace_existing_chunk_id(tmp_path: Path) -> None:
    """同名 chunk_id 再次 add → 旧 label mark_deleted、新 vector 落进索引。"""
    dim = 8
    store = _make_store(tmp_path, dim=dim)

    await store.add("c0", "d0", _unit_vec(1, dim))
    await store.add("c0", "d0", _unit_vec(50, dim))  # 重写

    # 用 seed=50 查 → 第一名应是新 c0；用 seed=1 查应低于 1（旧 vector 已删）
    h_new = await store.search(_unit_vec(50, dim), top_k=1)
    assert h_new[0][0] == "c0"
    assert h_new[0][1] > 0.99
    h_old = await store.search(_unit_vec(1, dim), top_k=1)
    assert h_old[0][0] == "c0"
    assert h_old[0][1] < 0.99  # 不再是自身


@pytest.mark.unit
async def test_dim_mismatch_on_reload_raises(tmp_path: Path) -> None:
    dim = 8
    store = _make_store(tmp_path, dim=dim)
    await store.add("c0", "d0", _unit_vec(1, dim))

    # 用错的 dim 重新打开 → 应抛 VectorStoreError
    with pytest.raises(VectorStoreError):
        _make_store(tmp_path, dim=16)


@pytest.mark.unit
async def test_empty_store_search_returns_empty(tmp_path: Path) -> None:
    dim = 8
    store = _make_store(tmp_path, dim=dim)
    hits = await store.search(_unit_vec(1, dim), top_k=5)
    assert hits == []


@pytest.mark.unit
async def test_compact_after_many_deletions(tmp_path: Path) -> None:
    """add 200 + delete 150 → 触发 compact；live=50；deleted_pending=0。"""
    dim = 8
    store = _make_store(tmp_path, dim=dim)

    items = [(f"d-c{i:04d}", f"d{i // 50}", _unit_vec(i + 1, dim)) for i in range(200)]
    await store.add_batch(items)
    assert await store.count() == 200

    # 删除 doc d0/d1/d2（150 chunks）
    n_del = 0
    for did in ("d0", "d1", "d2"):
        n_del += await store.delete_doc(did)
    assert n_del == 150

    stats = await store.stats()
    assert stats["n_vectors"] == 50
    # compact 应已触发（>10% deleted ratio），pending=0
    assert stats["n_deleted_pending_compact"] == 0


@pytest.mark.unit
async def test_existing_chunk_ids(tmp_path: Path) -> None:
    dim = 8
    store = _make_store(tmp_path, dim=dim)
    await store.add_batch(
        [
            ("a-c0", "a", _unit_vec(1, dim)),
            ("a-c1", "a", _unit_vec(2, dim)),
            ("b-c0", "b", _unit_vec(3, dim)),
        ]
    )
    ids = await store.existing_chunk_ids()
    assert ids == {"a-c0", "a-c1", "b-c0"}

    await store.delete_doc("a")
    ids2 = await store.existing_chunk_ids()
    assert ids2 == {"b-c0"}


@pytest.mark.unit
async def test_sidecar_is_valid_json(tmp_path: Path) -> None:
    dim = 8
    store = _make_store(tmp_path, dim=dim)
    await store.add("c0", "d0", _unit_vec(1, dim))
    sidecar = tmp_path / "vec" / "index.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["dim"] == dim
    assert data["next_label_id"] >= 1
    assert "c0" in data["chunk_to_label"]
