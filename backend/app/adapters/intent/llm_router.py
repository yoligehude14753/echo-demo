"""LLMIntentRouter：实现 IntentRouterPort。

策略（2026-05-28 简化版）：
1. ``@chat`` 显式逃生 → chat_no_rag（不查 RAG，纯 LLM 闲聊）
2. 强 RAG 信号词组（"基于附件" / "产品手册里" 等）→ search_rag（confidence=0.9）
3. 现有关键字命中（``@生成 PPT`` / ``@查...``）→ 对应意图（confidence=0.85）
4. 问句信号（含问号 / 含"什么/介绍" 等）→ search_rag（confidence=0.7）
5. 其余所有输入 → **默认 search_rag（=问 echo with RAG+web）**

用户 2026-05-28 反馈：「用户输入的文本默认是 @echo 的，要带知识库 + 上下文 +
网络」。旧设计在 5/6/7 步里调 Fast LLM 二次分类，Qwen3-1.7B 经常把
"@发 项目申报书模板到内部群" 这类未注册关键字误判成 chat_no_rag，导致 echo
不查 RAG 也不联网 → 回复显然没带上下文。

新设计：除 keyword_route 的明确命中外一律走 RAG/web 综合通道，让
retrieve_and_answer 自己决定查 RAG / web / both，再交主 LLM 综合回答。
_llm_classify / _extract_from_raw 当前未被默认路径调用，保留作为 future
扩展（如果以后要把 search_web/search_rag 进一步细分时再启用）。
"""

from __future__ import annotations

import json
import logging

from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.intent import (
    INTENT_TO_ARTIFACT_TYPE,
    SUPPORTED_INTENTS,
    IntentKind,
    IntentResult,
    keyword_route,
)
from app.schemas.llm import ChatMessage

logger = logging.getLogger(__name__)

_SYS_PROMPT = """你是 EchoDesk 桌面助手的意图路由器。

把用户输入分类为以下 12 类之一：

- search_web        : 用户想查最新资讯 / 价格 / 时事 / 联网
- search_rag        : 用户想问/查本地知识库（已上传 PDF / 会议 / 文档 / 工作区文件）
- generate_html     : 用户想生成 HTML 报告 / 网页 / 单文件可视化
- generate_pptx     : 用户想生成 PPT / 幻灯片
- generate_xlsx     : 用户想生成 Excel / 表格 / 财务模型 / DCF
- generate_word     : 用户想生成 Word 文档
- generate_markdown : 用户想生成 Markdown 笔记 / 报告 / 文档（.md）
- generate_pdf      : 用户想生成 PDF 报告 / 简历 / 单据
- generate_txt      : 用户想生成纯文本 / 列表 / 日志 / 代码片段（不带格式）
- summarize_meeting : 用户想总结当前会议 / 生成会议纪要
- chat_no_rag       : 用户显式声明只闲聊不用知识库（如以 "@chat" 开头）
- chat              : 真正的闲聊（你好/谢谢/再见/打招呼），既不查 RAG 也不生成

判定要点：
- 用户问"什么/为什么/怎么/介绍/讲讲/对比" → 多数是 search_rag
- 用户说"基于附件 / 根据文档 / 参考资料 / 在手册里" → 一定是 search_rag
- 用户只是寒暄 / 表达情绪 / 无具体诉求 → chat
- 会议开始/结束**不通过 @ 命令**，由 UI 状态栏点击 + 自动检测完成；
  "开始会议""结束会议"这类话归类为 chat。

严格输出 JSON：
{"kind": "<上述 12 选 1>", "confidence": 0.0~1.0, "rationale": "中文 ≤ 30 字"}

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
        # parse_at_prefix 暂不需要：默认就是问 echo（search_rag）

        # 先跑 keyword_route：明确意图（@生成 PPT / @查 / @chat / 强 RAG 信号词等）走对应分支
        hit = keyword_route(stripped)
        if hit is not None:
            kind, conf = hit
            params = self._params_for(kind, stripped, current_meeting_id)
            return IntentResult(
                kind=kind,
                confidence=conf,
                params=params,
                rationale="关键字命中",
            )

        # 用户 2026-05-28 反馈：「用户输入的文本是默认 @echo 的，要带知识库 +
        # 上下文 + 网络」——所以未命中明确意图的所有输入（含 `@发 项目申报书...`
        # 这类未注册关键字）都默认走 search_rag（=问 echo with RAG+web），
        # 不再调 Fast LLM 二次分类（旧路径把 `@发 ...` 误判成 chat_no_rag，
        # 既没用知识库也没联网，体验很差）。
        return IntentResult(
            kind="search_rag",
            confidence=None,
            rationale="默认问 echo（RAG + web + 会议上下文）",
            params={"question": stripped},
        )

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
        if kind in INTENT_TO_ARTIFACT_TYPE:
            params["brief"] = body
            params["artifact_type"] = INTENT_TO_ARTIFACT_TYPE[kind]
        elif kind in {"search_web", "search_rag"}:
            # 问句 / RAG 强信号若没有 @ 前缀，body 会等于完整 text；
            # 一切都好，下游 ragAsk(question) 用这个值检索。
            params["question"] = body or text.strip()
        elif kind == "summarize_meeting":
            params["meeting_id"] = current_meeting_id or ""
        else:  # chat / chat_no_rag
            params["text"] = body or text.strip()
        return params
