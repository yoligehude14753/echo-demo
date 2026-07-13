"""LLMIntentRouter：实现 IntentRouterPort。

策略：
1. ``@chat`` 显式逃生 → chat_no_rag（不查 RAG，纯 LLM 闲聊）
2. 明确调研/报告/方案产出 → 确定性 generate_*，附真实 artifact contract
3. 强 RAG 信号词组（"基于附件" / "产品手册里" 等）→ search_rag
4. 现有关键字/问句信号 → 对应意图
5. 未命中 → Fast LLM 短熔断分类；失败或非法输出立即切 Echo AI 主模型
6. 两个分类通道都失败 → chat 兜底

P4-fix-rag-chat（2026-05-28）：旧策略硬把"非 @ 前缀"全归 chat，导致用户
输入"请基于附件回答（XX.pdf）"被丢到 chat 兜底链路 → 不调 LLM 不查 RAG。
新策略在 no-@ 路径上也跑 keyword_route()，让 RAG 强信号 / 问句先于"chat
兜底"短路被识别。
"""

from __future__ import annotations

import json
import logging
import time

from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.intent import (
    INTENT_TO_ARTIFACT_TYPE,
    SUPPORTED_INTENTS,
    IntentKind,
    IntentResult,
    keyword_route,
    parse_at_prefix,
)
from app.schemas.llm import ChatMessage

logger = logging.getLogger(__name__)

_MAIN_ROUTE_FALLBACK_TIMEOUT_S = 8.0

_SYS_PROMPT = """你是 EchoDesk 桌面助手的意图路由器。

把用户输入分类为以下类别之一：

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
- agent_task        : 用户要 EchoDesk 后台执行长任务、复杂文件操作、浏览器/GUI 操作、深度调研或其它未对齐内置 skill 的 agent 任务
- chat_no_rag       : 用户显式声明只闲聊不用知识库（如以 "@chat" 开头）
- chat              : 真正的闲聊（你好/谢谢/再见/打招呼），既不查 RAG 也不生成

判定要点：
- 已对齐的生成类任务仍归 generate_*；未对齐 skill 的执行类任务归 agent_task
- 需要打开网页、浏览器操作、GUI 操作、跨多步研究、读写多个文件或长期执行 → agent_task
- 用户问"什么/为什么/怎么/介绍/讲讲/对比" → 多数是 search_rag
- 用户说"基于附件 / 根据文档 / 参考资料 / 在手册里" → 一定是 search_rag
- 用户只是寒暄 / 表达情绪 / 无具体诉求 → chat
- 会议开始/结束**不通过 @ 命令**，由 UI 状态栏点击 + 自动检测完成；
  "开始会议""结束会议"这类话归类为 chat。

严格输出 JSON：
{"kind": "<上述 13 选 1>", "confidence": 0.0~1.0, "rationale": "中文 ≤ 30 字"}

不要 markdown 围栏，不要解释。"""


class LLMIntentRouter:
    """实现 ports.intent.IntentRouterPort。

    - 关键字命中 → 0.85 置信度直接返回
    - 否则用 Fast LLM 出 JSON 标签
    """

    def __init__(self, settings: Settings, llm: LLMPort) -> None:
        self._settings = settings
        self._llm = llm
        self._fast_model = settings.llm_fast_model
        self._main_model = settings.llm_main_model
        self._fast_timeout_s = settings.llm_fast_classification_timeout_s

    async def route(
        self,
        text: str,
        *,
        current_meeting_id: str | None = None,
    ) -> IntentResult:
        started = time.perf_counter()
        stripped = text.strip()
        at_token = parse_at_prefix(stripped)
        has_at = at_token is not None or stripped.startswith("@")

        # P4-fix-rag-chat：所有路径先跑 keyword_route。
        # · 强 RAG 词组 / 问句即使没有 @ 也会命中 → 不再硬归 chat
        # · keyword_route 内部按"chat_no_rag > strong-rag > 普通 token > 问句"优先级
        hit = keyword_route(stripped)
        if hit is not None:
            kind, conf = hit
            params = self._params_for(kind, stripped, current_meeting_id)
            return self._finish_route(
                IntentResult(
                    kind=kind,
                    confidence=conf,
                    params=params,
                    rationale="关键字命中",
                ),
                started=started,
                source="deterministic",
            )

        # 非 @ 开头且关键字未命中 → 交给 LLM 路由。
        # ADR-012 的 claude_code/agent_task 决策不能靠关键词表；这里让 LLM
        # 判断是否为后台执行任务，闲聊仍会被分类为 chat。
        if not has_at:
            result = await self._llm_classify(stripped, current_meeting_id)
            return self._finish_route(result, started=started, source="llm")

        # @ 前缀但关键字未命中 → 走 Fast LLM 分类 + 兜底
        result = await self._llm_classify(stripped, current_meeting_id)
        return self._finish_route(result, started=started, source="llm")

    async def _llm_classify(
        self,
        stripped: str,
        current_meeting_id: str | None,
    ) -> IntentResult:
        attempts = [
            ("fast", self._fast_model, self._fast_timeout_s),
        ]
        if self._main_model != self._fast_model:
            attempts.append(("main_fallback", self._main_model, _MAIN_ROUTE_FALLBACK_TIMEOUT_S))

        for channel, model, timeout_s in attempts:
            attempt_started = time.perf_counter()
            try:
                resp = await self._llm.chat(
                    [
                        ChatMessage(role="system", content=_SYS_PROMPT),
                        ChatMessage(role="user", content=stripped),
                    ],
                    model=model,
                    max_tokens=80,
                    temperature=0.0,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                logger.warning(
                    "intent route model failed channel=%s model=%s elapsed_ms=%.1f error_type=%s",
                    channel,
                    model,
                    (time.perf_counter() - attempt_started) * 1000,
                    type(exc).__name__,
                )
                continue

            result = self._parse_llm_result(resp.content or "", stripped, current_meeting_id)
            if result is not None:
                logger.info(
                    "latency stage=route_classifier channel=%s model=%s elapsed_ms=%.1f",
                    channel,
                    model,
                    (time.perf_counter() - attempt_started) * 1000,
                )
                return result
            logger.warning(
                "intent route model returned invalid label channel=%s model=%s elapsed_ms=%.1f",
                channel,
                model,
                (time.perf_counter() - attempt_started) * 1000,
            )

        return IntentResult(kind="chat", confidence=0.3, rationale="分类服务失败兜底")

    def _parse_llm_result(
        self,
        content: str,
        stripped: str,
        current_meeting_id: str | None,
    ) -> IntentResult | None:
        raw = self._strip_code_fence(content.strip())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._extract_from_raw(raw, stripped, current_meeting_id)

        if not isinstance(data, dict):
            return None

        kind_raw = str(data.get("kind", "chat"))
        if kind_raw not in SUPPORTED_INTENTS:
            return None
        kind: IntentKind = kind_raw  # type: ignore[assignment]
        try:
            conf = min(1.0, max(0.0, float(data.get("confidence", 0.6))))
        except (TypeError, ValueError):
            conf = 0.6
        rationale = str(data.get("rationale", ""))[:80]
        params = self._params_for(kind, stripped, current_meeting_id)
        return IntentResult(kind=kind, confidence=conf, params=params, rationale=rationale)

    def _extract_from_raw(
        self,
        raw: str,
        stripped: str,
        current_meeting_id: str | None,
    ) -> IntentResult | None:
        lower = raw.lower()
        # 长标签优先，避免 ``chat`` 从 ``chat_no_rag`` 中被提前截出。
        for k in sorted(SUPPORTED_INTENTS, key=len, reverse=True):
            if k in lower:
                params = self._params_for(k, stripped, current_meeting_id)  # type: ignore[arg-type]
                return IntentResult(
                    kind=k,  # type: ignore[arg-type]
                    confidence=0.5,
                    params=params,
                    rationale="LLM 非 JSON 提取",
                )
        return None

    @staticmethod
    def _finish_route(
        result: IntentResult,
        *,
        started: float,
        source: str,
    ) -> IntentResult:
        logger.info(
            "latency stage=route source=%s kind=%s elapsed_ms=%.1f",
            source,
            result.kind,
            (time.perf_counter() - started) * 1000,
        )
        return result

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
            params["delivery"] = "artifact"
            params["output_contract"] = {
                "required": True,
                "artifact_type": INTENT_TO_ARTIFACT_TYPE[kind],
                "download": True,
            }
        elif kind in {"search_web", "search_rag"}:
            # 问句 / RAG 强信号若没有 @ 前缀，body 会等于完整 text；
            # 一切都好，下游 ragAsk(question) 用这个值检索。
            params["question"] = body or text.strip()
        elif kind == "summarize_meeting":
            params["meeting_id"] = current_meeting_id or ""
        elif kind == "agent_task":
            params["text"] = body or text.strip()
            params["title"] = (body or text.strip())[:42]
        else:  # chat / chat_no_rag
            params["text"] = body or text.strip()
        return params
