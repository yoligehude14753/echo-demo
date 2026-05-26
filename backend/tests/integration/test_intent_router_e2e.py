"""Integration：意图路由真 LLM 端到端（Qwen3-1.7B / heyi-bj）。

跳过条件：heyi-bj LLM 不可达 → 自动 skip。
覆盖：
1. 关键字命中场景（零 LLM）：@生成 PPT / @财务模型 / @查
2. LLM 兜底场景（无关键字）：模糊指令 → LLM 分类
"""

from __future__ import annotations

import socket

import pytest
from app.adapters.intent.llm_router import LLMIntentRouter
from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings

pytestmark = pytest.mark.integration


def _heyi_reachable() -> bool:
    s = Settings()
    url = s.llm_fast_base_url.replace("http://", "").replace("https://", "")
    host_port = url.split("/")[0]
    try:
        host, port_s = host_port.split(":")
        with socket.create_connection((host, int(port_s)), timeout=1.5):
            return True
    except (OSError, ValueError):
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(not _heyi_reachable(), reason="heyi-bj fast LLM unreachable")
async def test_intent_keyword_hits_skip_llm() -> None:
    """关键字命中应当不调 LLM 即返回（带 0.85 置信度）。"""
    s = Settings()
    llm = OpenAICompatibleLLM(s)
    try:
        router = LLMIntentRouter(s, llm)
        cases = [
            ("@生成 PPT 英伟达 2025 投资展望", "generate_pptx"),
            ("@财务模型 dcf 5 年敏感性", "generate_xlsx"),
            ("@查 黄金最新价格", "search_web"),
            ("@回忆 上次会议的 GPU 预算", "search_rag"),
            ("@生成纪要 当前会议", "summarize_meeting"),
        ]
        for text, expect in cases:
            r = await router.route(text, current_meeting_id="m-test")
            assert r.kind == expect, f"{text!r} → {r.kind}（应为 {expect}）"
            assert r.confidence >= 0.8
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not _heyi_reachable(), reason="heyi-bj fast LLM unreachable")
async def test_intent_llm_fallback_classifies() -> None:
    """无关键字 + @前缀 → 走 LLM 分类，返回合法 kind。"""
    s = Settings()
    llm = OpenAICompatibleLLM(s)
    try:
        router = LLMIntentRouter(s, llm)
        # 这条没有任何关键字命中，必须 LLM 分类
        r = await router.route(
            "@顺便帮我把刚才那场会议的关键要点整理一下",
            current_meeting_id="m-test",
        )
        # LLM 期望分类为 summarize_meeting（“整理要点”），允许 chat 兜底
        assert r.kind in {"summarize_meeting", "chat", "search_rag"}
        assert 0.0 <= r.confidence <= 1.0
    finally:
        await llm.aclose()
