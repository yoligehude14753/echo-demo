"""PR-16 unit：意图路由

覆盖：
- 关键字快速命中（不调 LLM）
- 非 @ 前缀 → chat
- 关键字未命中 → 走 mock LLM（含合法 JSON / 非法 JSON 兜底）
- LLM 失败 → chat 兜底
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.adapters.intent.llm_router import LLMIntentRouter
from app.config import Settings
from app.schemas.intent import (
    SUPPORTED_INTENTS,
    keyword_route,
    parse_at_prefix,
)
from app.schemas.llm import ChatMessage, LLMResponse


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "echo",
        rag_index_dir=tmp_path / "rag",
        llm_main_model="MiniMax-M2.7",
        llm_fast_model="qwen3-1.7b",
        yunwu_api_key="test",
        yunwu_base_url="http://localhost",
        heyi_base_url="http://localhost",
    )


class _MockLLM:
    """可控 chat 返回 / chat_stream 不实现。"""

    def __init__(self, content: str | None = None, *, raise_exc: Exception | None = None) -> None:
        self._content = content
        self._exc = raise_exc
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 120.0,
    ) -> LLMResponse:
        self.calls.append(messages)
        if self._exc is not None:
            raise self._exc
        return LLMResponse(content=self._content or "", model=model or "mock")

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 600.0,
    ) -> AsyncIterator[str]:  # pragma: no cover - 路由不用 stream
        raise NotImplementedError


@pytest.mark.unit
def test_parse_at_prefix() -> None:
    assert parse_at_prefix("@查英伟达 营收") == "查英伟达"
    assert parse_at_prefix("  @生成 PPT") == "生成"
    assert parse_at_prefix("没有 @") is None
    assert parse_at_prefix("@") is None
    assert parse_at_prefix("@   ") is None


@pytest.mark.unit
def test_keyword_route_hits_html() -> None:
    hit = keyword_route("@生成 HTML 周报")
    assert hit is not None
    kind, conf = hit
    assert kind == "generate_html"
    assert conf >= 0.8


@pytest.mark.unit
def test_keyword_route_hits_pptx() -> None:
    hit = keyword_route("@幻灯片 英伟达 2025")
    assert hit is not None
    assert hit[0] == "generate_pptx"


@pytest.mark.unit
def test_keyword_route_hits_xlsx() -> None:
    hit = keyword_route("@财务模型 dcf")
    assert hit is not None
    assert hit[0] == "generate_xlsx"


@pytest.mark.unit
def test_keyword_route_hits_summarize() -> None:
    hit = keyword_route("@生成纪要")
    assert hit is not None
    assert hit[0] == "summarize_meeting"


@pytest.mark.unit
def test_keyword_route_misses() -> None:
    assert keyword_route("帮我把这件事记下来") is None


# ── P4-M3：markdown / pdf / txt 三种新 intent 别名 ────────────────────────


@pytest.mark.unit
def test_keyword_route_hits_markdown() -> None:
    hit = keyword_route("@生成 Markdown 笔记")
    assert hit is not None
    assert hit[0] == "generate_markdown"


@pytest.mark.unit
def test_keyword_route_hits_markdown_via_biji_alias() -> None:
    """中文别名 '笔记' 命中 markdown。"""
    hit = keyword_route("@笔记 今天会议要点")
    assert hit is not None
    assert hit[0] == "generate_markdown"


@pytest.mark.unit
def test_keyword_route_hits_pdf() -> None:
    hit = keyword_route("@生成 PDF 月报")
    assert hit is not None
    assert hit[0] == "generate_pdf"


@pytest.mark.unit
def test_keyword_route_hits_pdf_lower_case() -> None:
    hit = keyword_route("@pdf 简历模板")
    assert hit is not None
    assert hit[0] == "generate_pdf"


@pytest.mark.unit
def test_keyword_route_hits_txt() -> None:
    hit = keyword_route("@生成 TXT 列表")
    assert hit is not None
    assert hit[0] == "generate_txt"


@pytest.mark.unit
def test_keyword_route_hits_txt_via_plaintext_alias() -> None:
    """中文别名 '纯文本' 命中 txt。"""
    hit = keyword_route("@纯文本 待办清单")
    assert hit is not None
    assert hit[0] == "generate_txt"


@pytest.mark.unit
def test_supported_intents_complete() -> None:
    # 12 类（P4-fix-rag-chat 起：新增 chat_no_rag 显式逃生路径）
    assert len(SUPPORTED_INTENTS) == 12
    assert "start_meeting" not in SUPPORTED_INTENTS
    assert "end_meeting" not in SUPPORTED_INTENTS
    assert "summarize_meeting" in SUPPORTED_INTENTS
    assert "generate_markdown" in SUPPORTED_INTENTS
    assert "generate_pdf" in SUPPORTED_INTENTS
    assert "generate_txt" in SUPPORTED_INTENTS
    assert "chat_no_rag" in SUPPORTED_INTENTS
    assert "chat" in SUPPORTED_INTENTS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_no_at_returns_chat(tmp_path: Path) -> None:
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("帮我写个周报", current_meeting_id=None)
    assert r.kind == "chat"
    assert llm.calls == []  # 完全跳过 LLM


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_no_at_returns_none_confidence(tmp_path: Path) -> None:
    """P4-fix（2026-05-28）：'无 @ 前缀' 是纯规则匹配路径，没有跑分类器。

    历史 bug：硬编码 confidence=1.0 → 前端显示 "置信度 100%"，给用户虚假置信感。
    回归断言：这条路径必须返回 confidence is None，前端按 null 显示 "规则匹配"，
    而不是把规则命中冒充成模型 100% 确信的分类结果。
    """
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("帮我写个周报", current_meeting_id=None)
    assert r.kind == "chat"
    assert r.confidence is None, (
        "无 @ 前缀 chat 路径不应硬编码 confidence=1.0；"
        f"实际返回 {r.confidence!r}（这条路径根本没跑分类器，confidence 没语义）"
    )
    assert "规则" in r.rationale or "前缀" in r.rationale
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_llm_classified_keeps_real_confidence(tmp_path: Path) -> None:
    """对比：经 LLM 真分类的路径应当返回有意义的 float 置信度（不是 None）。"""
    llm = _MockLLM(content='{"kind":"search_rag","confidence":0.78,"rationale":"想找之前的资料"}')
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@想找之前我们关于策略的讨论", current_meeting_id=None)
    assert r.kind == "search_rag"
    assert r.confidence is not None
    assert 0.7 <= r.confidence <= 0.9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_keyword_hit_keeps_real_confidence(tmp_path: Path) -> None:
    """对比：关键字命中路径仍然返回 0.85 float 置信度。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@生成 PPT 测试", current_meeting_id=None)
    assert r.kind == "generate_pptx"
    assert r.confidence is not None
    assert r.confidence >= 0.8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_keyword_hit_skips_llm(tmp_path: Path) -> None:
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@生成 PPT 英伟达 2025 投资展望", current_meeting_id="m1")
    assert r.kind == "generate_pptx"
    assert llm.calls == []
    assert r.params.get("artifact_type") == "pptx"
    assert "英伟达" in str(r.params.get("brief", ""))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_pdf_params(tmp_path: Path) -> None:
    """@生成 PDF 月报 → kind=generate_pdf, artifact_type='pdf'."""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@生成 PDF 5 月营收月报", current_meeting_id=None)
    assert r.kind == "generate_pdf"
    assert r.params.get("artifact_type") == "pdf"
    assert "营收" in str(r.params.get("brief", ""))
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_markdown_params(tmp_path: Path) -> None:
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@笔记 今天的会议要点", current_meeting_id=None)
    assert r.kind == "generate_markdown"
    assert r.params.get("artifact_type") == "markdown"
    assert "会议要点" in str(r.params.get("brief", ""))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_txt_params(tmp_path: Path) -> None:
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@生成 TXT 项目说明草稿", current_meeting_id=None)
    assert r.kind == "generate_txt"
    assert r.params.get("artifact_type") == "txt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_llm_json_ok(tmp_path: Path) -> None:
    # 用一个关键字命中不到的 @ 短句
    llm = _MockLLM(content='{"kind":"search_rag","confidence":0.78,"rationale":"想找之前的资料"}')
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@想找之前我们关于策略的讨论", current_meeting_id="m9")
    assert r.kind == "search_rag"
    assert 0.7 <= r.confidence <= 0.9
    assert r.params.get("question")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_llm_jsonp_with_fence(tmp_path: Path) -> None:
    llm = _MockLLM(content='```json\n{"kind":"chat","confidence":0.55,"rationale":"闲聊"}\n```')
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@顺便聊两句", current_meeting_id=None)
    assert r.kind == "chat"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_llm_non_json_extract(tmp_path: Path) -> None:
    # LLM 输出非 JSON 但含有合法 kind 关键字
    llm = _MockLLM(content="kind: search_web ，因为是最新消息")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@请把这个查清楚以后告诉我", current_meeting_id=None)
    assert r.kind == "search_web"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_llm_failure_falls_back_to_chat(tmp_path: Path) -> None:
    llm = _MockLLM(raise_exc=TimeoutError("net down"))
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@语义不明的句子触发 LLM 兜底", current_meeting_id=None)
    assert r.kind == "chat"
    assert r.confidence is not None
    assert r.confidence <= 0.5


# ── P4-fix-rag-chat（2026-05-28）：RAG 强信号 + 问句默认 RAG + chat_no_rag escape


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_strong_rag_phrase_overrides_no_at_chat(tmp_path: Path) -> None:
    """痛点截图复现：'请基于附件回答（XX.pdf）' 必须归 search_rag 而非 chat。

    旧逻辑：非 @ 前缀 → 硬归 chat → CommandBar 走兜底 toast → LLM 完全未调。
    新逻辑：keyword_route 在 no-@ 路径也跑，'基于附件' 命中强 RAG 信号 → search_rag。
    """
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route(
        "请基于附件回答（褐蚁AI工作站产品手册_260416.pdf）",
        current_meeting_id=None,
    )
    assert r.kind == "search_rag"
    assert r.confidence is not None
    assert r.confidence >= 0.85
    assert llm.calls == []
    assert r.params.get("question")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_question_mark_defaults_to_rag(tmp_path: Path) -> None:
    """'褐蚁的功能有哪些？' 没有 @ 前缀但是问句 → 默认走 RAG。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("褐蚁的功能有哪些？", current_meeting_id=None)
    assert r.kind == "search_rag"
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_question_word_intro_defaults_to_rag(tmp_path: Path) -> None:
    """'给我介绍下这个产品' 含"介绍" → 默认走 RAG。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("给我介绍下这个产品", current_meeting_id=None)
    assert r.kind == "search_rag"
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_greeting_remains_chat(tmp_path: Path) -> None:
    """'你好' 这种纯寒暄不含问句词 / RAG 信号 → 仍然走 chat（无 @ 前缀路径）。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("你好", current_meeting_id=None)
    assert r.kind == "chat"
    assert r.confidence is None  # 规则匹配路径
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_time_query_remains_chat(tmp_path: Path) -> None:
    """'现在几点' 没有问号 / 介绍词 / RAG 信号 → chat（避免误归 RAG）。

    注：实际场景下用户问"现在几点"通常想要联网，但当前 keyword_route 没有
    "几点"映射，保留旧行为（chat）以免误归 search_rag 浪费 RAG 检索。
    """
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("现在几点", current_meeting_id=None)
    assert r.kind == "chat"
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_chat_no_rag_explicit_escape(tmp_path: Path) -> None:
    """'@chat 你好' 显式声明纯闲聊 → chat_no_rag，跳过 RAG 检索。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@chat 你好", current_meeting_id=None)
    assert r.kind == "chat_no_rag"
    assert r.confidence is not None
    assert r.confidence >= 0.9
    assert r.params.get("text") == "你好"
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_chat_no_rag_escapes_even_with_question_mark(tmp_path: Path) -> None:
    """'@chat 现在几点？' 即便后面带问号也是 chat_no_rag（escape 优先级最高）。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("@chat 现在几点？", current_meeting_id=None)
    assert r.kind == "chat_no_rag"
    assert llm.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_genshe_pdf_keyword_priority(tmp_path: Path) -> None:
    """'根据文档 / 在资料里' 等中文表述同样命中 RAG。"""
    llm = _MockLLM(content="should not be called")
    router = LLMIntentRouter(_settings(tmp_path), llm)
    r = await router.route("根据文档说说这个产品", current_meeting_id=None)
    assert r.kind == "search_rag"
    # "根据文档" 命中强 RAG 短语，又包含 "说说" → 取强信号 0.9
    assert r.confidence is not None
    assert r.confidence >= 0.85
    assert llm.calls == []
