"""hnswlib cosine 索引 + (label_id → chunk_id) 映射的持久化封装。

设计要点（2026-05-28，rag_redesign_2026-05-28 §C.3 HybridRag 主链路）：

- 单一全局 hnswlib HNSW 索引（cosine space）；dim 在构造时由 EmbeddingPort.dim
  决定（bge-m3=1024、text-embedding-3-large=3072），首次落盘后写入 sidecar；
  重载时如发现 dim 不一致直接抛错（模型漂移由 HybridRag 上层负责重建）。

- sidecar JSON ``rag_vector_index/index.json``：
  - ``label_to_chunk``：``{label_id: {doc_id, chunk_id}}``
  - ``chunk_to_label``：``{chunk_id: label_id}`` 反查
  - ``doc_to_chunks``：``{doc_id: [chunk_id, ...]}`` 删除时按 doc 批删
  - ``deleted_labels``：mark_deleted 过的 label_id（用于统计 + 触发 compact）
  - ``next_label_id`` / ``dim`` / ``model_name`` / ``max_elements``

- 持久化策略：每次 add / add_batch / delete 后立即 ``save_index`` + 写 sidecar。
  1k-10k chunks 量级 save_index 单次 ~50-200ms；后续可加 batched flush（>10k 才必要）。

- 删除：调 ``mark_deleted(label_id)``（hnswlib 0.7+）。累积 mark_deleted 比例
  超阈值时调 ``_maybe_compact()`` 重建索引（全量重建，简化但 O(N)；1k chunks
  ~1s 量级可接受）。

- 容量：``init_max_elements=100_000``，写入接近上限时 ``resize_index(new_max*2)``。

- 并发：单个 ``asyncio.Lock`` 保护读写。hnswlib search 本身是线程安全的，但 save/
  add/resize 需要排他；为了简化先一把锁兜底（与 BM25Rag 同形）。

- 架构层级：``adapters/`` 层，``ports/`` 层禁止 import hnswlib（架构 fitness
  test ``backend/tests/arch/test_layer_dependencies.py``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.config import Settings

logger = logging.getLogger(__name__)


_SIDE_CAR_VERSION = 1
_DEFAULT_MAX_ELEMENTS = 100_000
_HNSW_M = 32
_HNSW_EF_CONSTRUCTION = 200
_HNSW_EF_QUERY = 50
_COMPACT_DELETED_RATIO = 0.10  # > 10% marked-deleted → 全量重建


class VectorStoreError(RuntimeError):
    """VectorStore 内部错误（dim 不匹配 / 持久化失败 / hnswlib 抛错）。"""


class VectorStore:
    """hnswlib cosine 索引 + label_id ↔ chunk_id 双向映射的持久化封装。

    生命周期：构造时尝试从磁盘加载；不存在 → 在 ``init_max_elements`` 容量上
    新建空索引；存在 → 还原索引 + sidecar。任何加载阶段错误直接抛
    ``VectorStoreError``（HybridRag 上层捕获并降级为纯 BM25）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        dim: int = 1024,
        index_dir: Path | None = None,
        init_max_elements: int = _DEFAULT_MAX_ELEMENTS,
    ) -> None:
        self._settings = settings
        self._dim = int(dim)
        self._index_dir = (
            index_dir
            if index_dir is not None
            else Path(settings.rag_index_dir).expanduser().parent / "rag_vector_index"
        )
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._bin_path = self._index_dir / "index.bin"
        self._sidecar_path = self._index_dir / "index.json"

        self._lock = asyncio.Lock()
        self._max_elements = init_max_elements
        self._next_label_id = 0
        self._label_to_chunk: dict[int, tuple[str, str]] = {}  # label → (doc_id, chunk_id)
        self._chunk_to_label: dict[str, int] = {}
        self._doc_to_chunks: dict[str, set[str]] = {}
        self._deleted_labels: set[int] = set()

        self._index: Any | None = None
        self._load_or_init()

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def index_dir(self) -> Path:
        return self._index_dir

    def _load_or_init(self) -> None:
        try:
            import hnswlib
        except ImportError as e:  # pragma: no cover - tests run with deps installed
            raise VectorStoreError(
                "hnswlib not installed; "
                "install via `pip install -r backend/requirements-extras-embedding.txt`"
            ) from e

        idx = hnswlib.Index(space="cosine", dim=self._dim)

        if self._bin_path.exists() and self._sidecar_path.exists():
            try:
                side = json.loads(self._sidecar_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise VectorStoreError(f"sidecar JSON unreadable: {e}") from e

            stored_dim = int(side.get("dim", 0))
            if stored_dim != self._dim:
                raise VectorStoreError(
                    f"vector store dim mismatch: stored={stored_dim} requested={self._dim}; "
                    f"upper layer (HybridRag) must rebuild index after model version drift"
                )
            self._max_elements = int(side.get("max_elements", _DEFAULT_MAX_ELEMENTS))
            self._next_label_id = int(side.get("next_label_id", 0))
            self._label_to_chunk = {
                int(k): (v["doc_id"], v["chunk_id"])
                for k, v in side.get("label_to_chunk", {}).items()
            }
            self._chunk_to_label = {k: int(v) for k, v in side.get("chunk_to_label", {}).items()}
            self._doc_to_chunks = {k: set(v) for k, v in side.get("doc_to_chunks", {}).items()}
            self._deleted_labels = {int(x) for x in side.get("deleted_labels", [])}
            try:
                idx.load_index(str(self._bin_path), max_elements=self._max_elements)
                idx.set_ef(_HNSW_EF_QUERY)
            except Exception as e:
                raise VectorStoreError(f"hnswlib load_index failed: {e}") from e
        else:
            idx.init_index(
                max_elements=self._max_elements,
                ef_construction=_HNSW_EF_CONSTRUCTION,
                M=_HNSW_M,
            )
            idx.set_ef(_HNSW_EF_QUERY)

        self._index = idx

    def _persist(self) -> None:
        """save_index → tmp → rename；sidecar 同样原子写。"""
        assert self._index is not None
        t0 = time.monotonic()
        tmp_bin = self._bin_path.with_suffix(".bin.tmp")
        tmp_side = self._sidecar_path.with_suffix(".json.tmp")
        self._index.save_index(str(tmp_bin))
        side = {
            "version": _SIDE_CAR_VERSION,
            "dim": self._dim,
            "max_elements": self._max_elements,
            "next_label_id": self._next_label_id,
            "label_to_chunk": {
                str(k): {"doc_id": v[0], "chunk_id": v[1]} for k, v in self._label_to_chunk.items()
            },
            "chunk_to_label": self._chunk_to_label,
            "doc_to_chunks": {k: sorted(v) for k, v in self._doc_to_chunks.items()},
            "deleted_labels": sorted(self._deleted_labels),
        }
        tmp_side.write_text(json.dumps(side, ensure_ascii=False), encoding="utf-8")
        tmp_bin.replace(self._bin_path)
        tmp_side.replace(self._sidecar_path)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > 500:
            logger.info(
                "vector_store persist: n=%d deleted=%d took=%.0fms",
                len(self._label_to_chunk),
                len(self._deleted_labels),
                elapsed_ms,
            )

    def _ensure_capacity(self, additional: int) -> None:
        """label_id 接近 max_elements → resize 翻倍。"""
        assert self._index is not None
        needed = self._next_label_id + additional
        if needed <= self._max_elements:
            return
        new_max = max(self._max_elements * 2, needed)
        try:
            self._index.resize_index(new_max)
        except Exception as e:
            raise VectorStoreError(f"hnswlib resize_index failed: {e}") from e
        self._max_elements = new_max

    def _validate_vector(self, vector: list[float]) -> NDArray[np.float32]:
        if len(vector) != self._dim:
            raise VectorStoreError(f"vector dim mismatch: got {len(vector)} expected {self._dim}")
        return np.asarray(vector, dtype=np.float32).reshape(1, -1)

    async def add(self, chunk_id: str, doc_id: str, vector: list[float]) -> None:
        """单条添加；重复 chunk_id 会替换为新 vector（先 mark_deleted 旧 label）。"""
        async with self._lock:
            await asyncio.to_thread(self._add_sync, [(chunk_id, doc_id, vector)])

    async def add_batch(self, items: list[tuple[str, str, list[float]]]) -> None:
        """批量添加；vectors 形如 [(chunk_id, doc_id, [floats…]), ...]。"""
        if not items:
            return
        async with self._lock:
            await asyncio.to_thread(self._add_sync, items)

    def _add_sync(self, items: list[tuple[str, str, list[float]]]) -> None:
        if not items:
            return
        assert self._index is not None

        # 拆分：已存在 chunk_id 走 "替换"（先 mark_deleted 旧 label）；新 chunk_id 走 append
        replace_old_labels: list[int] = []
        labels: list[int] = []
        vectors: list[NDArray[np.float32]] = []
        chunk_meta: list[tuple[str, str]] = []
        for chunk_id, doc_id, vec in items:
            arr = self._validate_vector(vec)
            old = self._chunk_to_label.get(chunk_id)
            if old is not None and old not in self._deleted_labels:
                replace_old_labels.append(old)
            label_id = self._next_label_id
            self._next_label_id += 1
            labels.append(label_id)
            vectors.append(arr)
            chunk_meta.append((chunk_id, doc_id))

        for old in replace_old_labels:
            with contextlib.suppress(Exception):
                self._index.mark_deleted(old)
            self._deleted_labels.add(old)

        self._ensure_capacity(0)  # next_label_id 已自增

        try:
            mat = np.concatenate(vectors, axis=0).astype(np.float32)
            self._index.add_items(mat, np.array(labels, dtype=np.int64))
        except Exception as e:
            raise VectorStoreError(f"hnswlib add_items failed: {e}") from e

        for label_id, (chunk_id, doc_id) in zip(labels, chunk_meta, strict=True):
            self._label_to_chunk[label_id] = (doc_id, chunk_id)
            self._chunk_to_label[chunk_id] = label_id
            self._doc_to_chunks.setdefault(doc_id, set()).add(chunk_id)

        self._maybe_compact()
        self._persist()

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        """返回 [(chunk_id, cosine_similarity_score)]，按 score 降序。

        hnswlib cosine space 返回的是 ``distance = 1 - cosine_similarity``，
        我们转回 similarity 让上层语义直观（越大越相近）。
        """
        if top_k <= 0:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._search_sync, vector, top_k)

    def _search_sync(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        assert self._index is not None
        live_count = len(self._label_to_chunk) - len(self._deleted_labels)
        if live_count <= 0:
            return []
        arr = self._validate_vector(vector)
        # hnswlib 要求 k <= ef；当 top_k 大于 ef_query 时临时抬一下 ef
        ef = max(_HNSW_EF_QUERY, top_k + 16)
        try:
            self._index.set_ef(ef)
            n = min(top_k, max(1, live_count))
            labels, distances = self._index.knn_query(arr, k=n)
        except Exception as e:
            raise VectorStoreError(f"hnswlib knn_query failed: {e}") from e

        out: list[tuple[str, float]] = []
        for label, dist in zip(labels[0], distances[0], strict=True):
            label_int = int(label)
            if label_int in self._deleted_labels:
                continue
            meta = self._label_to_chunk.get(label_int)
            if meta is None:
                continue
            similarity = 1.0 - float(dist)
            out.append((meta[1], similarity))
        return out

    async def delete_doc(self, doc_id: str) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._delete_doc_sync, doc_id)

    def _delete_doc_sync(self, doc_id: str) -> int:
        assert self._index is not None
        chunk_ids = self._doc_to_chunks.pop(doc_id, set())
        n_deleted = 0
        for chunk_id in chunk_ids:
            label_id = self._chunk_to_label.pop(chunk_id, None)
            if label_id is None or label_id in self._deleted_labels:
                continue
            try:
                self._index.mark_deleted(label_id)
            except Exception:
                continue
            self._deleted_labels.add(label_id)
            n_deleted += 1
        if n_deleted > 0:
            self._maybe_compact()
            self._persist()
        return n_deleted

    def _maybe_compact(self) -> None:
        total = len(self._label_to_chunk)
        deleted = len(self._deleted_labels)
        if total < 100 or deleted / max(total, 1) < _COMPACT_DELETED_RATIO:
            return
        self._compact_sync()

    def _compact_sync(self) -> None:
        """全量重建（live items only）。简化版；1k-10k chunks 在 < 1s 完成。"""
        assert self._index is not None
        try:
            import hnswlib
        except ImportError as e:  # pragma: no cover
            raise VectorStoreError("hnswlib not installed") from e

        t0 = time.monotonic()
        live_labels: list[int] = sorted(
            label for label in self._label_to_chunk if label not in self._deleted_labels
        )
        if not live_labels:
            # 全部删除 → 重建空索引
            new_idx = hnswlib.Index(space="cosine", dim=self._dim)
            new_idx.init_index(
                max_elements=self._max_elements,
                ef_construction=_HNSW_EF_CONSTRUCTION,
                M=_HNSW_M,
            )
            new_idx.set_ef(_HNSW_EF_QUERY)
            self._index = new_idx
            self._label_to_chunk = {}
            self._chunk_to_label = {}
            self._doc_to_chunks = {}
            self._deleted_labels.clear()
            self._next_label_id = 0
            return

        # 拉出仍存活的 vectors，分配紧凑的新 label
        try:
            old_vectors = self._index.get_items(live_labels, return_type="numpy")
        except Exception as e:
            raise VectorStoreError(f"hnswlib get_items failed during compact: {e}") from e

        new_idx = hnswlib.Index(space="cosine", dim=self._dim)
        new_idx.init_index(
            max_elements=self._max_elements,
            ef_construction=_HNSW_EF_CONSTRUCTION,
            M=_HNSW_M,
        )
        new_idx.set_ef(_HNSW_EF_QUERY)
        new_labels = np.arange(len(live_labels), dtype=np.int64)
        try:
            new_idx.add_items(np.asarray(old_vectors, dtype=np.float32), new_labels)
        except Exception as e:
            raise VectorStoreError(f"hnswlib add_items failed during compact: {e}") from e

        new_label_to_chunk: dict[int, tuple[str, str]] = {}
        new_chunk_to_label: dict[str, int] = {}
        new_doc_to_chunks: dict[str, set[str]] = {}
        for new_label, old_label in enumerate(live_labels):
            meta = self._label_to_chunk[old_label]
            new_label_to_chunk[new_label] = meta
            new_chunk_to_label[meta[1]] = new_label
            new_doc_to_chunks.setdefault(meta[0], set()).add(meta[1])

        self._index = new_idx
        self._label_to_chunk = new_label_to_chunk
        self._chunk_to_label = new_chunk_to_label
        self._doc_to_chunks = new_doc_to_chunks
        self._deleted_labels.clear()
        self._next_label_id = len(live_labels)
        logger.info(
            "vector_store compact: live=%d took=%.0fms",
            len(live_labels),
            (time.monotonic() - t0) * 1000,
        )

    async def count(self) -> int:
        """有效（未 mark_deleted）chunk 数。"""
        async with self._lock:
            return len(self._label_to_chunk) - len(self._deleted_labels)

    async def has_chunk(self, chunk_id: str) -> bool:
        async with self._lock:
            label = self._chunk_to_label.get(chunk_id)
            if label is None:
                return False
            return label not in self._deleted_labels

    async def existing_chunk_ids(self) -> set[str]:
        """回填 / 增量 ingest 时用：快速判断哪些 chunk_id 已写入。"""
        async with self._lock:
            return {
                chunk_id
                for chunk_id, label in self._chunk_to_label.items()
                if label not in self._deleted_labels
            }

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "dim": self._dim,
                "n_vectors": len(self._label_to_chunk) - len(self._deleted_labels),
                "n_deleted_pending_compact": len(self._deleted_labels),
                "n_docs": len(self._doc_to_chunks),
                "max_elements": self._max_elements,
                "index_dir": str(self._index_dir),
            }
