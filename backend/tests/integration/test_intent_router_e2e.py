"""Integration：意图路由真 V4 Flash 计划端到端。

跳过条件：主模型 endpoint 不可达 → 自动 skip。
覆盖：
1. 关键字命中场景：@生成 PPT / @财务模型 / @查 仍必须生成计划
2. 无关键字场景：模糊指令 → LLM 计划
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import pytest
from app.adapters.intent.llm_router import LLMIntentRouter
from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings

pytestmark = pytest.mark.integration


def _main_model_reachable() -> bool:
    s = Settings()
    parsed = urlparse(s.llm_main_base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.skipif(not _main_model_reachable(), reason="V4 Flash endpoint unreachable")
async def test_intent_keyword_hints_still_require_a_v4_flash_plan() -> None:
    """关键词只能提示候选；所有命令仍需主模型计划授权。"""
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
            assert isinstance(r.params.get("intent_plan"), dict)
            assert r.params["intent_plan"]["execution_target"] == "builtin_skill"
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.skipif(not _main_model_reachable(), reason="V4 Flash endpoint unreachable")
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
