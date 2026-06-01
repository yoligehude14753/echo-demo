"""HybridRag adapter 单测：mock embedding + vector store。

覆盖：
- RRF fusion 数学正确性（手算 1/(k+rank)）
- dense 抛错时降级为纯 BM25（不抛、log warning）
- ingest 时 dense 写失败的 graceful degradation（BM25 仍生效）
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import pytest
from app.adapters.embedding.errors import EmbeddingError
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.rag.hybrid import _RRF_K, HybridRag
from app.adapters.rag.vector_store import VectorStore
from app.config import Settings

# ---------- fakes ----------


class _FakeEmbedding:
    """实现 EmbeddingPort 的最小 fake；按 text → 固定 vector 字典查询。"""

    def __init__(
        self,
        *,
        dim: int = 8,
        vectors: dict[str, list[float]] | None = None,
        raise_on_encode: bool = False,
    ) -> None:
        self._dim = dim
        self._vectors = vectors or {}
        self._raise = raise_on_encode
        self.encode_calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "fake/embed"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def max_input_tokens(self) -> int:
        return 8192

    async def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        self.encode_calls.append(list(texts))
        if self._raise:
            raise EmbeddingError("fake encode failure")
        out: list[list[float]] = []
        for t in texts:
            if t in self._vectors:
                out.append(self._vectors[t])
            else:
                # 默认按 hash → 简单单位向量；不与其他文本平行
                seed = abs(hash(t)) % 1000 + 1
                raw = [(seed * 13 + i * 7 + 1) % 97 for i in range(self._dim)]
                norm = math.sqrt(sum(x * x for x in raw))
                out.append([x / norm for x in raw])
        return out

    async def health(self) -> bool:
        return not self._raise


def _unit_vec(seed: int, dim: int) -> list[float]:
    raw = [(seed * 13 + i * 7 + 1) % 97 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        rag_index_dir=tmp_path / "rag_index",
        embedding_batch_size=4,
        embedding_timeout_s=2.0,
    )


# ---------- ingest + query happy path ----------


@pytest.mark.unit
async def test_hybrid_ingest_query_basic(tmp_path: Path) -> None:
    """ingest 一个 md → BM25 入库 + dense 写入 → query 同时拿到 BM25 与 dense hits。"""
    settings = _make_settings(tmp_path)
    md = tmp_path / "note.md"
    md.write_text("人工智能 transformer 大模型综述。BM25 是关键词检索算法。", encoding="utf-8")

    bm25 = BM25Rag(settings)
    fake_emb = _FakeEmbedding(dim=8)
    vs = VectorStore(settings, dim=8, index_dir=tmp_path / "vec")
    hybrid = HybridRag(bm25, fake_emb, vs, settings)

    doc_id = await hybrid.ingest_file(str(md), doc_title="note", source="upload")
    assert doc_id

    # 现在 vector store 里应有 ≥1 vector
    assert await vs.count() >= 1
    # encode 调过 1 次（ingest path）
    assert len(fake_emb.encode_calls) == 1

    # 查询 → 应至少返回 1 个 chunk
    hits = await hybrid.query("BM25 检索", top_k=5)
    assert len(hits) >= 1
    # encode 第二次（query path）
    assert len(fake_emb.encode_calls) == 2


@pytest.mark.unit
async def test_hybrid_query_dense_failure_falls_back_to_bm25(tmp_path: Path) -> None:
    """dense encode 抛 EmbeddingError → 仍能返回 BM25 结果。"""
    settings = _make_settings(tmp_path)
    md = tmp_path / "note.md"
    md.write_text("人工智能 BM25 检索关键词命中。", encoding="utf-8")

    bm25 = BM25Rag(settings)
    vs = VectorStore(settings, dim=8, index_dir=tmp_path / "vec")
    # ingest 阶段先用一个 ok embedding 写 dense
    ok_emb = _FakeEmbedding(dim=8)
    hybrid_ok = HybridRag(bm25, ok_emb, vs, settings)
    await hybrid_ok.ingest_file(str(md), doc_title="note", source="upload")

    # 把 hybrid 换成 raise-on-encode 的 embedding
    raise_emb = _FakeEmbedding(dim=8, raise_on_encode=True)
    hybrid = HybridRag(bm25, raise_emb, vs, settings)
    hits = await hybrid.query("BM25 关键词", top_k=5)
    assert len(hits) >= 1  # BM25 alone 仍可返回
    # encode 被尝试过一次（query path）→ 抛错 → 转纯 BM25
    assert len(raise_emb.encode_calls) == 1


@pytest.mark.unit
async def test_hybrid_ingest_dense_failure_does_not_block(tmp_path: Path) -> None:
    """ingest 时 dense encode 抛错 → BM25 仍正常写入，doc 可被 BM25 query 命中。"""
    settings = _make_settings(tmp_path)
    md = tmp_path / "note.md"
    md.write_text("graceful degradation 关键测试段落。", encoding="utf-8")

    bm25 = BM25Rag(settings)
    raise_emb = _FakeEmbedding(dim=8, raise_on_encode=True)
    vs = VectorStore(settings, dim=8, index_dir=tmp_path / "vec")
    hybrid = HybridRag(bm25, raise_emb, vs, settings)

    doc_id = await hybrid.ingest_file(str(md), doc_title="note", source="upload")
    assert doc_id
    # vector store 仍空（dense 失败但 BM25 写入成功）
    assert await vs.count() == 0

    # 纯 BM25 仍能命中
    hits = await hybrid.query("graceful degradation", top_k=5)
    assert len(hits) >= 1


# ---------- RRF fusion 数学正确性 ----------


@pytest.mark.unit
def test_rrf_fusion_math() -> None:
    """手算：bm25 rank=[A,B,C]、dense rank=[B,A,D] → 期望排序 B > A > C ≈ D。"""
    from app.schemas.rag import RagChunk

    def _ch(cid: str) -> RagChunk:
        return RagChunk(doc_id="d", doc_title="d", chunk_id=cid, text=cid)

    bm25 = [_ch("A"), _ch("B"), _ch("C")]
    dense = [_ch("B"), _ch("A"), _ch("D")]
    fused = HybridRag._rrf_fuse(bm25, dense, top_k=4)
    ids = [c.chunk_id for c in fused]
    # 手算分数：
    #   A: 1/(60+1)  + 1/(60+2)  = 1/61 + 1/62
    #   B: 1/(60+2)  + 1/(60+1)  = 1/62 + 1/61   (= A)
    #   C: 1/(60+3)              = 1/63
    #   D:               1/(60+3) = 1/63
    # A == B（同分）；CD 同分。B 与 A 谁先取决于 dict 顺序——只校验前 2 个集合 = {A,B}
    assert set(ids[:2]) == {"A", "B"}
    assert set(ids[2:4]) == {"C", "D"}
    # 第一位的 score 应是 1/61 + 1/62
    expected = 1.0 / (_RRF_K + 1) + 1.0 / (_RRF_K + 2)
    assert fused[0].score == pytest.approx(expected)


@pytest.mark.unit
async def test_hybrid_rrf_promotes_dense_only_hit(tmp_path: Path) -> None:
    """场景：query 用 dense 命中、BM25 miss → RRF 拿出 dense 结果。

    构造方式：写两段文本。query 与 BM25 共享 keyword 的段叫 X；与 dense 接近
    （但 BM25 无 token 重叠）的段叫 Y。我们手工把 Y 段的 embedding 设成与
    query embedding 一致 → dense 必返回 Y。
    """
    settings = _make_settings(tmp_path)
    dim = 8

    # 文档 1：BM25 上能匹配 query 关键词 "transformer"
    doc1 = tmp_path / "doc1.md"
    doc1.write_text("transformer 是关键词检索可命中段。", encoding="utf-8")
    # 文档 2：BM25 上完全无 token 重叠，但 dense 同向量
    doc2 = tmp_path / "doc2.md"
    doc2.write_text("seq2seq encoder decoder 自注意力机制说明段。", encoding="utf-8")

    bm25 = BM25Rag(settings)
    # query 与 doc2 文本共享同一个 fake vector → dense 必命中 doc2
    shared = _unit_vec(42, dim)
    vectors = {
        "transformer 是关键词检索可命中段。": _unit_vec(99, dim),
        "seq2seq encoder decoder 自注意力机制说明段。": shared,
        "transformer query": shared,  # query 用到的文本
    }
    fake_emb = _FakeEmbedding(dim=dim, vectors=vectors)
    vs = VectorStore(settings, dim=dim, index_dir=tmp_path / "vec")
    hybrid = HybridRag(bm25, fake_emb, vs, settings)

    await hybrid.ingest_file(str(doc1), doc_title="doc1", source="upload")
    await hybrid.ingest_file(str(doc2), doc_title="doc2", source="upload")

    hits = await hybrid.query("transformer query", top_k=5)
    doc_ids = [h.doc_id for h in hits]
    # 期望 RRF 同时拉出两个 doc
    doc1_id = next(c.doc_id for c in bm25._chunks if "transformer" in c.text)
    doc2_id = next(c.doc_id for c in bm25._chunks if "seq2seq" in c.text)
    assert doc1_id in doc_ids
    assert doc2_id in doc_ids
