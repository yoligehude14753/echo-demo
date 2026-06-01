"""HybridRag adapter：BM25 + dense embedding via RRF fusion。

设计（rag_redesign_2026-05-28 §C.3）：

- ``ingest_*``：先走 BM25 入库（同步，不可失败），再异步写 dense vector。
  dense 失败（embedding adapter/timeout/vector store）→ log warning，
  **不阻塞 ingest**；BM25 仍可独立检索（graceful degradation 硬要求）。

- ``query``：
  1) BM25.query(top_k=N) → bm25_chunks
  2) embedding.encode([query], is_query=True) → qvec
  3) vector_store.search(qvec, top_k=N) → dense_hits = [(chunk_id, sim)]
  4) Reciprocal Rank Fusion：score(c) = Σ 1 / (k + rank_i(c))，k=60；
     dense / bm25 各自按 rank 计数，取并集后排序，截断 top-K。
  5) 缺失 metadata 的 dense 结果通过 BM25 chunk index 补全 RagChunk 元数据。

- 单 adapter 实现 ``RagPort`` 全部 7 个方法；BM25 部分原样代理。

- 失败（``EmbeddingError`` / ``VectorStoreError`` / timeout）→ 单次查询降级为
  纯 BM25，``log.warning`` 一行不抛错。

- 模型版本漂移：上层（factory / lifespan）发现 ``embedding.dim != vector_store.dim``
  应重建 vector store。本 adapter 不主动处理（让上层 force-rebuild 更显式）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.adapters.embedding.errors import EmbeddingError
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.rag.vector_store import VectorStore, VectorStoreError
from app.config import Settings
from app.ports.embedding import EmbeddingPort
from app.schemas.rag import RagChunk

logger = logging.getLogger(__name__)


_RRF_K = 60
"""RRF 常数 k。Cormack 等 2009 的经典推荐值；对单 ranker 的 weight 不敏感时
``1 / (60 + rank)`` 让 top-1 与 top-10 的差距既不会被 top-1 一家独大、也
不至于 head/tail 差距过小。"""

_DENSE_QUERY_TIMEOUT_FLOOR_S = 30.0
"""单查询 dense 通道外层超时下限。

adapter.encode 自己有 ``settings.embedding_timeout_s``，这里的 wait_for 只防止
to_thread / hnswlib 异常挂死。bge-m3 CPU 首次/连续 query 实测可能超过 5s，不能用
过短的外层 timeout 把 healthy dense 通道误判为失败。
"""


class HybridRag:
    """BM25 + dense RRF fusion；对外实现 ``ports.rag.RagPort``。"""

    def __init__(
        self,
        bm25: BM25Rag,
        embedding: EmbeddingPort,
        vector_store: VectorStore,
        settings: Settings,
    ) -> None:
        self._bm25 = bm25
        self._embedding = embedding
        self._vector_store = vector_store
        self._settings = settings
        self._batch_size = max(1, int(settings.embedding_batch_size))
        self._encode_timeout_s = float(settings.embedding_timeout_s)
        self._dense_query_timeout_s = max(
            _DENSE_QUERY_TIMEOUT_FLOOR_S,
            self._encode_timeout_s + 5.0,
        )

    # ---------- BM25 透传 ----------

    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        doc_id = await self._bm25.ingest_pdf(file_path, doc_title=doc_title)
        await self._write_dense_for_doc(doc_id)
        return doc_id

    async def ingest_file(
        self,
        file_path: str,
        doc_title: str | None = None,
        *,
        source: str = "upload",
        source_path: str | None = None,
    ) -> str:
        doc_id = await self._bm25.ingest_file(
            file_path,
            doc_title=doc_title,
            source=source,
            source_path=source_path,
        )
        await self._write_dense_for_doc(doc_id)
        return doc_id

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        doc_id = await self._bm25.ingest_meeting(meeting_id, transcript, title)
        # meeting 是"删旧再插"语义；先把 vector store 里同 doc_id 残留清掉
        await self._safe_delete_doc(doc_id)
        await self._write_dense_for_doc(doc_id)
        return doc_id

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> str:
        doc_id = await self._bm25.ingest_ambient_segment(
            text,
            captured_at=captured_at,
            audio_ref=audio_ref,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
        )
        # ambient 每段追加一个 chunk；只补这次新增的 chunk 的 vector
        await self._write_dense_for_doc(doc_id, only_missing=True)
        return doc_id

    async def delete(self, doc_id: str) -> None:
        await self._bm25.delete(doc_id)
        await self._safe_delete_doc(doc_id)

    async def find_by_source_path(self, source_path: str) -> str | None:
        return await self._bm25.find_by_source_path(source_path)

    async def list_docs(self) -> list[dict[str, object]]:
        return await self._bm25.list_docs()

    # ---------- query：BM25 + dense → RRF ----------

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        if not query.strip():
            return []

        t0 = time.monotonic()
        bm25_chunks = await self._bm25.query(query, top_k=top_k)

        dense_chunks = await self._dense_query(query, top_k=top_k)
        if not dense_chunks:
            return bm25_chunks

        fused = self._rrf_fuse(bm25_chunks, dense_chunks, top_k=top_k)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > 1000:
            logger.info(
                "hybrid query elapsed=%.0fms bm25=%d dense=%d fused=%d",
                elapsed_ms,
                len(bm25_chunks),
                len(dense_chunks),
                len(fused),
            )
        return fused

    async def _dense_query(self, query: str, *, top_k: int) -> list[RagChunk]:
        """encode query → vector_store.search → 补全 metadata。失败返 []。"""
        try:
            vectors = await asyncio.wait_for(
                self._embedding.encode(
                    [query],
                    batch_size=1,
                    timeout_s=self._encode_timeout_s,
                    is_query=True,
                ),
                timeout=self._dense_query_timeout_s,
            )
        except (EmbeddingError, TimeoutError) as e:
            logger.warning("hybrid dense encode failed → BM25 only: %s", e)
            return []
        except Exception as e:  # 兜底任何意外
            logger.warning("hybrid dense encode unexpected error → BM25 only: %s", e)
            return []
        if not vectors or not vectors[0]:
            return []

        try:
            hits = await self._vector_store.search(vectors[0], top_k=top_k)
        except VectorStoreError as e:
            logger.warning("hybrid dense search failed → BM25 only: %s", e)
            return []

        if not hits:
            return []

        chunk_index = self._bm25_chunk_index()
        out: list[RagChunk] = []
        for chunk_id, sim in hits:
            base = chunk_index.get(chunk_id)
            if base is None:
                # vector store 有，但 BM25 已经 delete（不一致窗口）→ 跳过
                continue
            out.append(
                RagChunk(
                    doc_id=base.doc_id,
                    doc_title=base.doc_title,
                    chunk_id=base.chunk_id,
                    text=base.text,
                    score=float(sim),
                    metadata=base.metadata,
                )
            )
        return out

    def _bm25_chunk_index(self) -> dict[str, RagChunk]:
        """从 BM25 取 chunk_id → RagChunk 映射（无锁；BM25Rag 内部用 list 维护）。"""
        return {c.chunk_id: c for c in self._bm25._chunks}

    @staticmethod
    def _rrf_fuse(
        bm25_chunks: list[RagChunk],
        dense_chunks: list[RagChunk],
        *,
        top_k: int,
    ) -> list[RagChunk]:
        """Reciprocal Rank Fusion：score = Σ 1 / (k + rank)。

        - 两 ranker 同等权重（spike 报告里没有可靠权重证据时的中立默认）
        - 返回的 RagChunk score 字段记录 RRF 综合分（不是 BM25 原分），
          便于上层 ``retrieve_and_answer`` 的 doc-cap / grep boost 在
          *同口径* 分数上做后排
        """
        rrf_scores: dict[str, float] = {}
        merged: dict[str, RagChunk] = {}

        for rank, c in enumerate(bm25_chunks):
            rrf_scores[c.chunk_id] = rrf_scores.get(c.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            merged.setdefault(c.chunk_id, c)
        for rank, c in enumerate(dense_chunks):
            rrf_scores[c.chunk_id] = rrf_scores.get(c.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            merged.setdefault(c.chunk_id, c)

        ranked = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        out: list[RagChunk] = []
        for chunk_id, score in ranked:
            base = merged[chunk_id]
            out.append(
                RagChunk(
                    doc_id=base.doc_id,
                    doc_title=base.doc_title,
                    chunk_id=base.chunk_id,
                    text=base.text,
                    score=float(score),
                    metadata=base.metadata,
                )
            )
        return out

    # ---------- dense 写入辅助 ----------

    async def _write_dense_for_doc(self, doc_id: str, *, only_missing: bool = False) -> None:
        """把 BM25 里 doc_id 的所有 chunk encode + 写 vector store。失败仅 log。"""
        chunks = [c for c in self._bm25._chunks if c.doc_id == doc_id]
        if not chunks:
            return

        if only_missing:
            try:
                existing = await self._vector_store.existing_chunk_ids()
            except Exception as e:
                logger.warning("hybrid existing_chunk_ids failed: %s", e)
                existing = set()
            chunks = [c for c in chunks if c.chunk_id not in existing]
            if not chunks:
                return

        texts = [c.text for c in chunks]
        try:
            vectors = await self._embedding.encode(
                texts,
                batch_size=self._batch_size,
                timeout_s=self._encode_timeout_s,
            )
        except EmbeddingError as e:
            logger.warning(
                "hybrid dense ingest skip doc=%s (encode failed, n=%d): %s",
                doc_id,
                len(chunks),
                e,
            )
            return
        except Exception as e:
            logger.warning(
                "hybrid dense ingest skip doc=%s (unexpected, n=%d): %s",
                doc_id,
                len(chunks),
                e,
            )
            return

        items = [(c.chunk_id, c.doc_id, v) for c, v in zip(chunks, vectors, strict=True)]
        try:
            await self._vector_store.add_batch(items)
        except VectorStoreError as e:
            logger.warning(
                "hybrid dense ingest skip doc=%s (vector_store add failed, n=%d): %s",
                doc_id,
                len(chunks),
                e,
            )

    async def _safe_delete_doc(self, doc_id: str) -> None:
        try:
            await self._vector_store.delete_doc(doc_id)
        except Exception as e:
            logger.warning("hybrid vector_store delete_doc failed: %s", e)

    # ---------- 诊断 ----------

    def stats(self) -> dict[str, Any]:
        """同 BM25Rag.stats() 接口；附加 vector store 计数。"""
        base = self._bm25.stats()
        # vector_store.stats() 是 async；这里 stats() 是 sync（与 BM25 兼容），
        # 取内部计数器即可（accept 轻微不精确：deleted_pending 不计入主结果）。
        vec_count = len(self._vector_store._label_to_chunk) - len(
            self._vector_store._deleted_labels
        )
        base["vector_count"] = vec_count
        base["vector_enabled"] = True
        base["embedding_model"] = self._embedding.model_name
        return base
