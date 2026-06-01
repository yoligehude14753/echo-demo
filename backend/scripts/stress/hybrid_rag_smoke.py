"""真链路验证：HybridRag（BM25 + dense 向量 + hnswlib）在本机能跑通。

用真 embedding（yunwu text-embedding-3-large，fallback 路；无需本地 torch）写入
hnswlib 向量库，再用"几乎无关键词重叠、仅语义相关"的查询验证 dense 检索确实生效
（纯 BM25 难命中）。临时目录，不污染真实库。

用法：
    .venv/bin/python scripts/stress/hybrid_rag_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from app.adapters.rag.bm25 import BM25Rag
from app.adapters.rag.factory import build_rag
from app.adapters.rag.hybrid import HybridRag
from app.config import get_settings


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hybrid_smoke_"))
    base = get_settings()
    settings = base.model_copy(
        update={
            "storage_dir": tmp,
            "rag_index_dir": tmp / "rag_index",
            "embedding_enabled": True,
            "embedding_main_provider": "yunwu",  # 无 torch，直接用云 embedding 主路
            "embedding_fallback_provider": "yunwu",
        }
    )
    (tmp / "rag_index").mkdir(parents=True, exist_ok=True)

    rag = build_rag(settings)
    print(f"build_rag → {type(rag).__name__}")
    if not isinstance(rag, HybridRag):
        print("FAIL: 期望 HybridRag，实际是 BM25-only（embedding/hnswlib 没生效）")
        return 1

    # 两篇语义迥异的文档
    await rag.ingest_meeting("m-photo", "光合作用是绿色植物借助叶绿素吸收阳光，把二氧化碳和水合成有机物的过程。", "生物")
    await rag.ingest_meeting("m-chain", "区块链是一种去中心化的分布式账本技术，依靠密码学保证交易不可篡改。", "技术")

    # 查询：和"光合作用"文档语义相关，但几乎不共享关键词（树木/太阳/养分 vs 光合作用/阳光/有机物）
    q = "树木怎样利用太阳来获取生长所需的养分"
    hits = await rag.query(q, top_k=2)
    print(f"\nquery: {q}")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] doc={h.doc_id} score={h.score:.4f} :: {h.text[:40]}")

    ok = bool(hits) and hits[0].doc_id.endswith("m-photo")
    print(f"\n{'OK' if ok else 'FAIL'}: 混合检索命中正确文档 = {ok}（top1={hits[0].doc_id if hits else None}）")

    # 对照：纯 BM25 对同一查询的表现（证明 dense 带来的增量）
    bm = BM25Rag(settings)
    await bm.ingest_meeting("m-photo", "光合作用是绿色植物借助叶绿素吸收阳光，把二氧化碳和水合成有机物的过程。", "生物")
    await bm.ingest_meeting("m-chain", "区块链是一种去中心化的分布式账本技术，依靠密码学保证交易不可篡改。", "技术")
    bm_hits = await bm.query(q, top_k=2)
    bm_top = bm_hits[0].doc_id if bm_hits else "(无结果)"
    print(f"对照 BM25-only top1 = {bm_top}（hits={len(bm_hits)}）")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
