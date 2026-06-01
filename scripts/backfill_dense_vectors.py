#!/usr/bin/env python3
"""把现有 BM25 索引里的所有 chunks 回填到 dense vector store。

用途（phase5-hybrid-rag, 2026-05-28）：
- 老 backend 一直是纯 BM25，磁盘上有几十/几百个 ``{doc_id}.json``（含 chunks）
- HybridRag 上线后，dense 通道是空的——必须一次性回填，否则前期 query 的
  dense 通道一直返回 0 个 hit，相当于退化成纯 BM25
- 跑过一次后再 ingest 走的是 HybridRag.ingest_*，会同步写两边，不再需要回填

策略：
- 直接读 ``settings.rag_index_dir``（同 BM25Rag 的目录）
- 对每个 chunk 调 ``EmbeddingPort.encode``（默认 batch=32）
- 写入 ``VectorStore.add_batch``
- 跳过已在 vector store 里的 chunk_id（断点续跑安全）
- 单 batch encode 超时不致命，跳过该 batch 继续

用法：
    .venv/bin/python scripts/backfill_dense_vectors.py
    .venv/bin/python scripts/backfill_dense_vectors.py --dry-run
    .venv/bin/python scripts/backfill_dense_vectors.py --batch-size 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

REPO_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(REPO_BACKEND))


from app.adapters.embedding import BgeM3LocalEmbedding, EmbeddingRouter, YunwuOpenAIEmbedding  # noqa: E402
from app.adapters.embedding.errors import EmbeddingError  # noqa: E402
from app.adapters.rag.vector_store import VectorStore, VectorStoreError  # noqa: E402
from app.config import Settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def _load_chunks(index_dir: Path) -> list[tuple[str, str, str]]:
    """从 BM25 索引文件夹 ``rag_index/*.json`` 全量读出 chunks。

    返回 [(doc_id, chunk_id, text), ...]，按 doc_id+chunk_id 排序保证可复现。
    """
    out: list[tuple[str, str, str]] = []
    for f in sorted(index_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("跳过无法解析的 doc 文件 %s: %s", f.name, e)
            continue
        doc_id = data.get("doc_id") or f.stem
        for c in data.get("chunks", []):
            chunk_id = c.get("chunk_id")
            text = (c.get("text") or "").strip()
            if not chunk_id or not text:
                continue
            out.append((doc_id, chunk_id, text))
    return out


def _build_embedding() -> EmbeddingRouter:
    s = Settings()
    primary = None
    if BgeM3LocalEmbedding is not None:
        try:
            primary = BgeM3LocalEmbedding(s)
        except Exception as e:  # noqa: BLE001
            log.warning("bge-m3 init 失败 → 回退 yunwu: %s", e)
    fallback = YunwuOpenAIEmbedding(s)
    return EmbeddingRouter(primary=primary, fallback=fallback)


async def _backfill(
    *,
    dry_run: bool,
    batch_size: int,
    batch_timeout_s: float,
    progress_every: int,
) -> int:
    settings = Settings()
    index_dir = Path(settings.rag_index_dir).expanduser()
    if not index_dir.exists():
        log.error("BM25 index dir 不存在: %s", index_dir)
        return 1

    embedding = _build_embedding()
    log.info("embedding provider=%s dim=%d", embedding.active_provider, embedding.dim)

    vector_store = VectorStore(settings, dim=embedding.dim)
    log.info("vector store at %s dim=%d", vector_store.index_dir, vector_store.dim)

    chunks = _load_chunks(index_dir)
    log.info("BM25 chunks 全集: n=%d (来自 %s)", len(chunks), index_dir)

    existing = await vector_store.existing_chunk_ids()
    pending = [(d, c, t) for (d, c, t) in chunks if c not in existing]
    log.info("已 dense 写入: %d；待回填: %d", len(existing), len(pending))

    if dry_run:
        log.info("--dry-run：仅计数，不实际回填")
        return 0

    written = 0
    failed_batches = 0
    t_total = time.monotonic()
    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        texts = [text for _, _, text in batch]
        t0 = time.monotonic()
        try:
            vectors = await embedding.encode(
                texts,
                batch_size=batch_size,
                timeout_s=batch_timeout_s,
            )
        except EmbeddingError as e:
            log.warning(
                "batch %d-%d encode 失败（跳过）: %s",
                batch_start,
                batch_start + len(batch),
                e,
            )
            failed_batches += 1
            continue

        items = [
            (chunk_id, doc_id, vec)
            for (doc_id, chunk_id, _), vec in zip(batch, vectors, strict=True)
        ]
        try:
            await vector_store.add_batch(items)
        except VectorStoreError as e:
            log.warning(
                "batch %d-%d 写 vector store 失败（跳过）: %s",
                batch_start,
                batch_start + len(batch),
                e,
            )
            failed_batches += 1
            continue
        written += len(items)
        elapsed = time.monotonic() - t0
        if (batch_start // batch_size) % max(1, progress_every // batch_size) == 0:
            log.info(
                "进度 %d/%d (此 batch %d 项 %.1fs, %.1f items/s)",
                written + (batch_start - written),  # 已尝试 = 已写入 + 跳过
                len(pending),
                len(items),
                elapsed,
                len(items) / max(elapsed, 1e-3),
            )

    total = time.monotonic() - t_total
    log.info(
        "完成: 写入 %d 个 vector, 失败 batches=%d, 总耗时 %.1fs (%.1f items/s)",
        written,
        failed_batches,
        total,
        written / max(total, 1e-3),
    )

    stats = await vector_store.stats()
    log.info("vector store final stats: %s", json.dumps(stats, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="只统计待回填数量, 不写入")
    p.add_argument("--batch-size", type=int, default=32, help="encode batch size, 默认 32")
    p.add_argument(
        "--batch-timeout-s",
        type=float,
        default=60.0,
        help="单 batch encode 超时, 默认 60s",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="打印进度间隔（条数, 实际按 batch 粒度近似）",
    )
    args = p.parse_args()
    return asyncio.run(
        _backfill(
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            batch_timeout_s=args.batch_timeout_s,
            progress_every=args.progress_every,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
