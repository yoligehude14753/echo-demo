"""LLMIntentRouter：实现 IntentRouterPort。

策略：
1. 优先关键字命中（confidence=0.85）：覆盖 70%+ 演示场景，零 LLM 开销
2. 关键字未命中 → 走 Fast LLM (Qwen3-1.7B) 分类
3. LLM 输出 JSON 解析失败 / 服务不可达 → fallback 到 chat
"""

from __future__ import annotations

import json
import logging

from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.intent import (
    SUPPORTED_INTENTS,
    IntentKind,
    IntentResult,
    keyword_route,
    parse_at_prefix,
)
from app.schemas.llm import ChatMessage

logger = logging.getLogger(__name__)

_SYS_PROMPT = """你是 Echo 桌面助手的意图路由器。

把用户输入分类为以下 9 类之一：

- search_web        : 用户想查最新资讯 / 价格 / 时事 / 联网
- search_rag        : 用户想回忆之前会议 / 文档 / 本地知识库
- generate_html     : 用户想生成 HTML 报告 / 网页 / 单文件可视化
- generate_pptx     : 用户想生成 PPT / 幻灯片
- generate_xlsx     : 用户想生成 Excel / 表格 / 财务模型 / DCF
- generate_word     : 用户想生成 Word 文档
- summarize_meeting : 用户想总结当前会议 / 生成会议纪要
- start_meeting     : 用户想开始新会议 / 建会议
- chat              : 兜底，其它任何聊天对话

严格输出 JSON：
{"kind": "<上述 9 选 1>", "confidence": 0.0~1.0, "rationale": "中文 ≤ 30 字"}

不要 markdown 围栏，不要解释。"""


class LLMIntentRouter:
    """实现 ports.intent.IntentRouterPort。

    - 关键字命中 → 0.85 置信度直接返回
    - 否则用 Fast LLM (Qwen3-1.7B) 出 JSON 标签
    """

    def __init__(self, settings: Settings, llm: LLMPort) -> None:
        self._settings = settings
        self._llm = llm
        self._fast_model = settings.llm_fast_model

    async def route(
        self,
        text: str,
        *,
        current_meeting_id: str | None = None,
    ) -> IntentResult:
        stripped = text.strip()
        # 非 @ 开头直接走 chat（不调 LLM）
        at_token = parse_at_prefix(stripped)
        if at_token is None and not stripped.startswith("@"):
            return IntentResult(kind="chat", confidence=1.0, rationale="无 @ 前缀")

        # 关键字快速路由
        hit = keyword_route(stripped)
        if hit is not None:
            kind, conf = hit
            params = self._params_for(kind, stripped, current_meeting_id)
            return IntentResult(kind=kind, confidence=conf, params=params, rationale="关键字命中")

        # LLM 分类 + 兜底
        return await self._llm_classify(stripped, current_meeting_id)

    async def _llm_classify(
        self,
        stripped: str,
        current_meeting_id: str | None,
    ) -> IntentResult:
        try:
            resp = await self._llm.chat(
                [
                    ChatMessage(role="system", content=_SYS_PROMPT),
                    ChatMessage(role="user", content=stripped),
                ],
                model=self._fast_model,
                max_tokens=80,
                temperature=0.0,
                timeout_s=15.0,
            )
        except Exception as e:
            logger.warning("intent LLM failed, fallback to chat: %s", e)
            return IntentResult(kind="chat", confidence=0.3, rationale="LLM 失败兜底")

        raw = self._strip_code_fence((resp.content or "").strip())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._extract_from_raw(raw, stripped, current_meeting_id)

        kind_raw = str(data.get("kind", "chat"))
        if kind_raw not in SUPPORTED_INTENTS:
            return IntentResult(kind="chat", confidence=0.2, rationale=f"非法 kind: {kind_raw}")
        kind: IntentKind = kind_raw  # type: ignore[assignment]
        conf = float(data.get("confidence", 0.6))
        rationale = str(data.get("rationale", ""))[:80]
        params = self._params_for(kind, stripped, current_meeting_id)
        return IntentResult(kind=kind, confidence=conf, params=params, rationale=rationale)

    def _extract_from_raw(
        self,
        raw: str,
        stripped: str,
        current_meeting_id: str | None,
    ) -> IntentResult:
        lower = raw.lower()
        for k in SUPPORTED_INTENTS:
            if k in lower:
                params = self._params_for(k, stripped, current_meeting_id)  # type: ignore[arg-type]
                return IntentResult(
                    kind=k,  # type: ignore[arg-type]
                    confidence=0.5,
                    params=params,
                    rationale="LLM 非 JSON 提取",
                )
        return IntentResult(kind="chat", confidence=0.2, rationale="LLM 解析失败")

    @staticmethod
    def _strip_code_fence(raw: str) -> str:
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw[3:]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return raw

    @staticmethod
    def _params_for(
        kind: IntentKind,
        text: str,
        current_meeting_id: str | None,
    ) -> dict[str, object]:
        # 剥掉 @prefix（@keyword 后到第一个空格为止视为指令；后面是真正 brief）。
        # 若 @ 后没有空格，则把整段作为 brief（用户没显式分指令/正文）。
        body = text.lstrip()
        if body.startswith("@"):
            after = body[1:]
            space_idx = -1
            for i, ch in enumerate(after):
                if ch in {" ", "\t"}:
                    space_idx = i
                    break
            body = after[space_idx + 1 :] if space_idx >= 0 else after
        body = body.strip()
        params: dict[str, object] = {}
        if kind in {
            "generate_html",
            "generate_pptx",
            "generate_xlsx",
            "generate_word",
        }:
            params["brief"] = body
            params["artifact_type"] = {
                "generate_html": "html",
                "generate_pptx": "pptx",
                "generate_xlsx": "xlsx",
                "generate_word": "word",
            }[kind]
        elif kind in {"search_web", "search_rag"}:
            params["question"] = body
        elif kind in {"summarize_meeting", "start_meeting"}:
            params["meeting_id"] = current_meeting_id or ""
        else:  # chat
            params["text"] = body or text.strip()
        return params
