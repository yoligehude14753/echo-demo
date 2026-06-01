"""retrieve_and_answer use_case 单测：mock LLM/RAG/Web。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.schemas.rag import RagChunk, WebHit
from app.use_cases.retrieve_and_answer import (
    _AMBIENT_BM25_PENALTY,
    _DEFAULT_RAG_TOP_K,
    _DOC_CHUNK_CAP,
    _PROMPT_RENDER_TOP_N,
    _rerank_diverse_with_priority_and_grep_boost,
    retrieve_and_answer,
)


def _mk(doc_id: str, text: str, score: float, source: str) -> RagChunk:
    return RagChunk(
        doc_id=doc_id,
        doc_title=doc_id,
        chunk_id=f"{doc_id}-c",
        text=text,
        score=score,
        metadata={"source": source, "kind": source},
    )


@pytest.mark.unit
def test_conversation_query_lets_ambient_rank_above_doc() -> None:
    """跨对话查询修复：问"上午说到X"时，含 X 的 ambient 段不应被文档硬压到后面。

    旧实现按 (source优先级, score) 排序，ambient 永远排在所有文档之后 → 查不到。
    新实现以相关度为主键 + 对话类查询给 ambient 加分。
    """
    # 真实场景：用户复述会上说过的话，ambient 转写里就含这些词（grep 精确命中）。
    ambient = _mk(
        "ambient-20260601",
        "上午说到CPU和GPU的异构方案是什么，主要是王文新在讲硬件",
        3.0,
        "ambient",
    )
    doc = _mk("manual.pdf", "本产品支持多种网络协议与安全特性，适用于企业部署", 5.0, "pdf")
    ranked = _rerank_diverse_with_priority_and_grep_boost(
        [doc, ambient], "上午说到CPU和GPU的异构方案是什么"
    )
    # ambient 必须被检索到，且排在不相关文档之前
    assert ambient in ranked
    assert ranked.index(ambient) < ranked.index(doc)


@pytest.mark.unit
def test_factual_query_still_prefers_doc_over_ambient_chatter() -> None:
    """普通事实查询：文档仍略优先于无关 ambient 闲聊（未被本次修复破坏）。"""
    chatter = _mk("ambient-20260601", "今天天气不错我们出去走走吃个饭", 3.0, "ambient")
    doc = _mk("spec.pdf", "网络协议规格：支持 TCP/UDP 与 TLS 加密传输", 3.0, "pdf")
    ranked = _rerank_diverse_with_priority_and_grep_boost(
        [chatter, doc], "网络协议规格是什么"
    )
    assert ranked.index(doc) < ranked.index(chatter)


class FakeLLM:
    def __init__(
        self, classify_label: str = "either", answer_chunks: list[str] | None = None
    ) -> None:
        self.classify_label = classify_label
        self.answer_chunks = answer_chunks or ["答", "复"]
        self.classify_calls: list[str] = []
        self.stream_messages: list[ChatMessage] | None = None

    async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
        # 分类器调用
        self.classify_calls.append(messages[-1].content)
        return LLMResponse(
            content=self.classify_label,
            model="qwen3",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=12.0,
        )

    async def chat_stream(self, messages: list[ChatMessage], **_: Any) -> AsyncIterator[str]:
        self.stream_messages = list(messages)
        for c in self.answer_chunks:
            yield c


class FakeRag:
    def __init__(self, chunks: list[RagChunk]) -> None:
        self.chunks = chunks
        self.query_count = 0

    async def query(self, q: str, *, top_k: int = 5) -> list[RagChunk]:
        self.query_count += 1
        return list(self.chunks)

    async def ingest_pdf(self, path: str, doc_title: str | None = None) -> str:
        return "fake"

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        return f"meeting-{meeting_id}"

    async def delete(self, doc_id: str) -> None:
        return None


class FakeWeb:
    def __init__(self, hits: list[WebHit]) -> None:
        self.hits = hits
        self.search_count = 0

    async def search(self, q: str, *, top_n: int = 5) -> list[WebHit]:
        self.search_count += 1
        return list(self.hits)


# === 基本路由 ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rag_only_branch_skips_web() -> None:
    llm = FakeLLM(classify_label="rag")
    rag = FakeRag(
        [RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="本地证据 1", score=0.8)]
    )
    web = FakeWeb([WebHit(title="x", url="u", snippet="y")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="本地 PDF 提到什么?",
    )
    chunks = [c async for c in out.chunks]
    assert "".join(chunks) == "答复"
    assert rag.query_count == 1
    assert web.search_count == 0
    assert out.retrieval.chosen_source == "rag"
    assert len(out.retrieval.rag_chunks) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_only_branch_skips_rag() -> None:
    llm = FakeLLM(classify_label="web")
    rag = FakeRag([RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="本地", score=0.5)])
    web = FakeWeb([WebHit(title="今日新闻", url="https://news/", snippet="...", source="tavily")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="今天有什么新闻?",
    )
    _ = [c async for c in out.chunks]
    assert web.search_count == 1
    assert rag.query_count == 0
    assert out.retrieval.chosen_source == "web"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_either_branch_runs_both() -> None:
    llm = FakeLLM(classify_label="either")
    rag = FakeRag([RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="local")])
    web = FakeWeb([WebHit(title="x", url="u", snippet="y")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="ambiguous query",
    )
    _ = [c async for c in out.chunks]
    assert rag.query_count == 1
    assert web.search_count == 1
    assert out.retrieval.chosen_source == "both"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_classifier_garbage_falls_back_to_either() -> None:
    llm = FakeLLM(classify_label="garbage output 我不懂")
    rag = FakeRag([])
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="x",
    )
    _ = [c async for c in out.chunks]
    assert rag.query_count == 1
    assert web.search_count == 1


# === ambient 降权 ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ambient_chunks_are_deprioritized_below_pdf() -> None:
    """用户 2026-05-28：「rag无效」根因是 BM25 给 ambient 长文档 score=11.5
    霸榜，褐蚁 PDF score=3.75 被挤出 top-5。

    D-new (rag_redesign_2026-05-28) 后 _AMBIENT_BM25_PENALTY=0.4 + source 优先级,
    无论 ambient raw score 多高,都排在 PDF/workspace 之后。
    """
    llm = FakeLLM(classify_label="rag")
    chunks_returned = [
        RagChunk(
            doc_id="ambient-20260528",
            doc_title="Ambient 2026-05-28",
            chunk_id="ambient-20260528-c0011",
            text="今天大家好像在聊别的",
            score=11.5,
            metadata={"source": "ambient", "kind": "ambient"},
        ),
        RagChunk(
            doc_id="pdf-1",
            doc_title="褐蚁说明书",
            chunk_id="pdf-1-c0",
            text="褐蚁 AI 工作站本地大模型工作站，开箱即用",
            score=3.75,
            metadata={"source": "workspace"},
        ),
        RagChunk(
            doc_id="pdf-2",
            doc_title="褐蚁手册",
            chunk_id="pdf-2-c0",
            text="褐蚁产品手册技术规格",
            score=3.75,
            metadata={"source": "upload"},
        ),
    ]
    rag = FakeRag(chunks_returned)
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="褐蚁竞品调研",
    )
    _ = [c async for c in out.chunks]
    assert out.retrieval.rag_chunks[0].metadata.get("source") in {"workspace", "upload"}
    assert out.retrieval.rag_chunks[-1].metadata.get("source") == "ambient"


@pytest.mark.unit
def test_ambient_penalty_is_0_4() -> None:
    """D-new 调到 0.4;配合 b=0.5 + doc-cap=12 + grep boost,足够压制 ambient。"""
    assert _AMBIENT_BM25_PENALTY == 0.4


# === 召回放大 / 渲染容量 ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_rag_top_k_is_1000() -> None:
    """D-new (rag_redesign_2026-05-28):粗召回放大到 1000。

    用户原话:「起码能覆盖 1000 个对话,100 个文件,这还是最低标准」。
    rank_bm25 在 10k chunks 量级 ≈ 50ms,token 不是瓶颈,质量优先。
    """
    assert _DEFAULT_RAG_TOP_K == 1000

    llm = FakeLLM(classify_label="rag")

    class _SpyRag(FakeRag):
        def __init__(self) -> None:
            super().__init__([])
            self.requested_top_k: int | None = None

        async def query(self, q: str, *, top_k: int = 5) -> list[RagChunk]:
            self.requested_top_k = top_k
            return []

    rag = _SpyRag()
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="任何问题",
    )
    _ = [c async for c in out.chunks]
    assert rag.requested_top_k == 1000, f"默认 top_k 应该是 1000,实际 {rag.requested_top_k}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_doc_chunk_cap_12() -> None:
    """D-new (rag_redesign_2026-05-28):同 doc_id 在 rerank 后最多保留 12 chunks。

    场景:某 ambient daily 含 30 chunks 都被 BM25 召回;没有 cap 时会霸榜
    prompt 全部 80 槽位,把 PDF 挤光。cap=12 后该 doc 最多 12 个进 prompt。
    """
    assert _DOC_CHUNK_CAP == 12

    llm = FakeLLM(classify_label="rag")
    hot_chunks = [
        RagChunk(
            doc_id="doc-hot",
            doc_title="hot",
            chunk_id=f"doc-hot-c{i:03d}",
            text=f"hot chunk number {i}",
            score=10.0 - i * 0.01,
            metadata={"source": "workspace"},
        )
        for i in range(30)
    ]
    rag = FakeRag(hot_chunks)
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="任意问题",
    )
    _ = [c async for c in out.chunks]
    rag_chunks = out.retrieval.rag_chunks
    hot_count = sum(1 for c in rag_chunks if c.doc_id == "doc-hot")
    assert hot_count == 12, f"doc-hot 应被 cap 在 12,实际 {hot_count}"
    # prompt 体内也应只出现 12 个 [doc:doc-hot- 前缀
    body = llm.stream_messages[0].content
    rendered = body.count("[doc:doc-hot-")
    assert rendered == 12, f"prompt 应渲染 12 个 doc-hot chunk,实际 {rendered}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_prompt_renders_80_chunks() -> None:
    """D-new (rag_redesign_2026-05-28):粗召回 1000 → rerank 后渲染 80 chunks。

    构造 200 个不同 doc 的候选(避开 doc-cap),期望 prompt 出现 80 个 [doc: 标记。
    """
    assert _PROMPT_RENDER_TOP_N == 80

    llm = FakeLLM(classify_label="rag")
    chunks_returned = [
        RagChunk(
            doc_id=f"doc-{i:03d}",
            doc_title=f"t{i:03d}",
            chunk_id=f"doc-{i:03d}-c00",
            text=f"content text {i}",
            score=200.0 - i,
            metadata={"source": "workspace"},
        )
        for i in range(200)
    ]
    rag = FakeRag(chunks_returned)
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="任意问题",
    )
    _ = [c async for c in out.chunks]
    assert len(out.retrieval.rag_chunks) == 80
    body = llm.stream_messages[0].content
    rendered = body.count("[doc:doc-")
    assert rendered == 80, f"prompt 应渲染 80 个 chunk,实际 {rendered}"


# === lost-in-the-middle 重排 ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_lost_in_middle_reorder() -> None:
    """D-new (rag_redesign_2026-05-28):top-20 放头、21-40 放尾、41-80 放中间。

    构造 80 个独立 doc 的 chunks,score 单调递减(CHUNK-00 最高、CHUNK-79 最低)。
    经 _reorder_for_long_context(head=20, tail=20) 后:
    - prompt 头 20 段 = CHUNK-00..CHUNK-19
    - prompt 中 40 段 = CHUNK-40..CHUNK-79
    - prompt 尾 20 段 = CHUNK-20..CHUNK-39
    """
    llm = FakeLLM(classify_label="rag")
    chunks_returned = [
        RagChunk(
            doc_id=f"doc-{i:02d}",
            doc_title=f"t{i:02d}",
            chunk_id=f"doc-{i:02d}-c00",
            text=f"MARK-{i:02d}",
            score=80.0 - i,
            metadata={"source": "workspace"},
        )
        for i in range(80)
    ]
    rag = FakeRag(chunks_returned)
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="任意问题",
    )
    _ = [c async for c in out.chunks]
    body = llm.stream_messages[0].content

    positions = {f"MARK-{i:02d}": body.find(f"MARK-{i:02d}") for i in range(80)}
    for tag, pos in positions.items():
        assert pos > 0, f"{tag} 未出现在 prompt 中"

    # head (0-19) 应全部早于 middle (40-79) 与 tail (20-39)
    for i in range(20):
        for j in list(range(20, 40)) + list(range(40, 80)):
            assert positions[f"MARK-{i:02d}"] < positions[f"MARK-{j:02d}"], (
                f"head MARK-{i:02d} 应在 MARK-{j:02d} 前"
            )

    # middle (40-79) 应早于 tail (20-39)
    for i in range(40, 80):
        for j in range(20, 40):
            assert positions[f"MARK-{i:02d}"] < positions[f"MARK-{j:02d}"], (
                f"middle MARK-{i:02d} 应在 tail MARK-{j:02d} 前"
            )

    # 抽样:MARK-00 最先、MARK-39 最后、MARK-40 落在 head 之后 middle 起始处
    head_end = max(positions[f"MARK-{i:02d}"] for i in range(20))
    middle_start = min(positions[f"MARK-{i:02d}"] for i in range(40, 80))
    assert head_end < middle_start
    assert positions["MARK-00"] == min(positions.values())
    assert positions["MARK-39"] == max(positions.values())


# === grep-style 字面提升 ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_grep_substring_exact_boost() -> None:
    """D-new (rag_redesign_2026-05-28):chunk 文本规范化后包含 query 整串 → +2.0。

    针对"褐蚁"、"FY26-Q3-XPL" 这种专有名词 query 的 fallback 精确匹配。
    """
    llm = FakeLLM(classify_label="rag")
    # 二者 raw score 相同;A 含 query 完整子串 → +2.0 boost;B 仅含分散字
    a_chunk = RagChunk(
        doc_id="doc-a",
        doc_title="A",
        chunk_id="doc-a-c0",
        text="本报告详细分析了褐蚁竞品调研的具体方法与样本范围。",
        score=1.0,
        metadata={"source": "workspace"},
    )
    b_chunk = RagChunk(
        doc_id="doc-b",
        doc_title="B",
        chunk_id="doc-b-c0",
        text="褐蚁是一家公司,我们做过一些行业调研,但本段没有竞品讨论。",
        score=1.0,
        metadata={"source": "workspace"},
    )
    rag = FakeRag([b_chunk, a_chunk])  # 故意把 A 放后面,验证靠 grep boost 翻盘
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="褐蚁竞品调研",
    )
    _ = [c async for c in out.chunks]
    ranked = out.retrieval.rag_chunks
    assert ranked[0].doc_id == "doc-a", (
        f"含完整 query 子串的 A 应排首位,实际首位 {ranked[0].doc_id}"
    )
    assert ranked[1].doc_id == "doc-b"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_grep_keyword_majority_boost() -> None:
    """D-new (rag_redesign_2026-05-28):≥2/3 keyword 命中 → +0.5;否则不加分。

    构造 A 含全部 3 keywords(分散)、B 仅含 1 keyword,验证 A 排前。
    """
    llm = FakeLLM(classify_label="rag")
    # 注意 A.text 不能包含 "褐蚁竞品调研" 子串,否则会触发 +2.0 而非 +0.5
    a_chunk = RagChunk(
        doc_id="doc-a",
        doc_title="A",
        chunk_id="doc-a-c0",
        text="褐蚁的最新产品做了一些竞品分析,以及深入的市场调研。",
        score=1.0,
        metadata={"source": "workspace"},
    )
    b_chunk = RagChunk(
        doc_id="doc-b",
        doc_title="B",
        chunk_id="doc-b-c0",
        text="市场上的常见竞品分析方法分类。",  # 只命中 "竞品"
        score=1.0,
        metadata={"source": "workspace"},
    )
    rag = FakeRag([b_chunk, a_chunk])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=FakeWeb([]),
        question="褐蚁竞品调研",
    )
    _ = [c async for c in out.chunks]
    ranked = out.retrieval.rag_chunks
    assert ranked[0].doc_id == "doc-a", f"含 3/3 keyword 的 A 应排首位,实际首位 {ranked[0].doc_id}"


# === inline_context 与 citations(原有保留) ===


@pytest.mark.asyncio
@pytest.mark.unit
async def test_prompt_includes_inline_context() -> None:
    """2026-05-28：inline_context（当前会议转录）拼到 prompt 让 Echo 感知上下文。"""
    llm = FakeLLM(classify_label="rag")
    rag = FakeRag([])
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="他们刚才同意做什么",
        inline_context="说话人1 · 那就先做三所试点\n说话人2 · 好",
    )
    _ = [c async for c in out.chunks]
    assert llm.stream_messages is not None
    body = llm.stream_messages[0].content
    assert "三所试点" in body
    assert "当前会议上下文" in body


@pytest.mark.asyncio
@pytest.mark.unit
async def test_prompt_omits_inline_context_when_empty() -> None:
    """没传 inline_context 时段标仍在但内容为 '(无)'，prompt 不崩。"""
    llm = FakeLLM(classify_label="either")
    rag = FakeRag([])
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="x",
    )
    _ = [c async for c in out.chunks]
    assert llm.stream_messages is not None
    body = llm.stream_messages[0].content
    assert "(无)" in body  # inline_context 为空时占位


@pytest.mark.asyncio
@pytest.mark.unit
async def test_prompt_includes_citations() -> None:
    llm = FakeLLM(classify_label="rag")
    rag = FakeRag(
        [
            RagChunk(
                doc_id="pdf-1",
                doc_title="财报",
                chunk_id="c1",
                text="2024 年营收 100 亿",
                score=0.9,
            )
        ]
    )
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="财报数据?",
    )
    _ = [c async for c in out.chunks]
    assert llm.stream_messages is not None
    body = llm.stream_messages[0].content
    assert "2024 年营收 100 亿" in body
    assert "[doc:c1" in body
    assert "财报" in body
