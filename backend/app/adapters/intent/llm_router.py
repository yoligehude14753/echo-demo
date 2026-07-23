"""Main-model-only, fail-closed planning gate for every chat dispatch."""

from __future__ import annotations

import json
import logging
import time

from pydantic import ValidationError

from app.adapters.intent.prompts import (
    BUILTIN_INTENT_PLAN_SYSTEM_PROMPT,
    build_builtin_intent_plan_user_prompt,
)
from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.intent import (
    BUILTIN_SKILL_INTENTS,
    INTENT_TO_ARTIFACT_TYPE,
    MAX_INTENT_CONTEXT_ITEMS,
    BuiltinIntentPlan,
    IntentKind,
    IntentResult,
    keyword_route,
)
from app.schemas.llm import ChatMessage

logger = logging.getLogger(__name__)

_INTENT_PLAN_TIMEOUT_S = 12.0
_INTENT_PLAN_MIN_CONFIDENCE = 0.7
_V4_FLASH_MODEL_ID = "deepseek-v4-flash"


class LLMIntentRouter:
    """The main-model-only gate for every chat-originated action."""

    def __init__(self, settings: Settings, llm: LLMPort) -> None:
        self._settings = settings
        self._llm = llm
        self._main_model = settings.llm_main_model

    async def route(
        self,
        text: str,
        *,
        current_meeting_id: str | None = None,
        available_context: list[str] | None = None,
    ) -> IntentResult:
        started = time.perf_counter()
        stripped = text.strip()
        planning_context = list(available_context or [])[:MAX_INTENT_CONTEXT_ITEMS]
        if current_meeting_id:
            # Keep the plan grounded in the selected meeting without exposing
            # an opaque internal identifier to the model provider.
            planning_context = planning_context[: MAX_INTENT_CONTEXT_ITEMS - 1]
            planning_context.append("当前会议：已选定，可用于会议总结")
        # 所有用户输入（包括显式 @ 命令）均由主模型生成严格计划。关键词只给
        # 计划器提供候选提示；它绝不构成执行开关或前端模板选择依据。
        result = await self._plan_builtin_intent(
            stripped,
            current_meeting_id=current_meeting_id,
            available_context=planning_context,
        )
        return self._finish_route(result, started=started, source="main_intent_plan")

    async def _plan_builtin_intent(
        self,
        stripped: str,
        *,
        current_meeting_id: str | None,
        available_context: list[str],
    ) -> IntentResult:
        """Only the configured V4 Flash main model can authorize a dispatch."""

        if self._main_model.strip().lower() != _V4_FLASH_MODEL_ID:
            logger.warning("builtin intent plan blocked: main model is not V4 Flash")
            return self._intent_plan_failure(stripped)

        hint = keyword_route(stripped)
        candidate_intents = sorted(BUILTIN_SKILL_INTENTS)
        if hint is not None and hint[0] in BUILTIN_SKILL_INTENTS:
            candidate_intents.remove(hint[0])
            candidate_intents.insert(0, hint[0])

        try:
            response = await self._llm.chat(
                [
                    ChatMessage(role="system", content=BUILTIN_INTENT_PLAN_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=build_builtin_intent_plan_user_prompt(
                            stripped, available_context, candidate_intents
                        ),
                    ),
                ],
                model=self._main_model,
                max_tokens=1_600,
                temperature=0.0,
                timeout_s=_INTENT_PLAN_TIMEOUT_S,
            )
            plan = BuiltinIntentPlan.model_validate_json((response.content or "").strip())
            if any(item not in available_context for item in plan.available_context):
                raise ValueError("plan introduced unavailable context")
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(
                "builtin intent plan invalid model=%s error_type=%s",
                self._main_model,
                type(exc).__name__,
            )
            return self._intent_plan_failure(stripped)
        except Exception as exc:
            logger.warning(
                "builtin intent plan failed model=%s error_type=%s",
                self._main_model,
                type(exc).__name__,
            )
            return self._intent_plan_failure(stripped)

        ready = (
            plan.execution_target != "clarification"
            and not plan.missing_constraints
            and not plan.clarification_questions
            and plan.confidence >= _INTENT_PLAN_MIN_CONFIDENCE
            and plan.execution_authorized
        )
        plan_payload = plan.model_dump(mode="json")
        params: dict[str, object] = {
            "intent_plan": plan_payload,
            "ready_to_execute": ready,
            "plan_status": "ready" if ready else "clarification_required",
            "execution_target": plan.execution_target,
            "required_clarification": "" if ready else self._clarification_for(plan),
            "assumption_draft": plan.assumptions,
        }
        if plan.execution_target == "builtin_skill" and plan.builtin_intent:
            kind: IntentKind = plan.builtin_intent  # type: ignore[assignment]
        elif plan.execution_target == "claude_code_runtime":
            kind = "agent_task"
        else:
            kind = "chat"
        if ready:
            params.update(self._params_for(kind, stripped, current_meeting_id))
        return IntentResult(
            kind=kind,
            confidence=plan.confidence,
            params=params,
            rationale="主模型结构化 intent plan",
        )

    @staticmethod
    def _clarification_for(plan: BuiltinIntentPlan) -> str:
        if plan.clarification_questions:
            return plan.clarification_questions[0]
        if plan.missing_constraints:
            missing = "、".join(plan.missing_constraints[:4])
            return f"开始执行前，请补充：{missing}。"
        return "开始执行前，请确认目标、范围和所需资料。"

    @staticmethod
    def _intent_plan_failure(text: str) -> IntentResult:
        return IntentResult(
            kind="chat",
            confidence=0.0,
            params={
                "ready_to_execute": False,
                "plan_status": "failed",
                "delivery": "clarification",
                "execution_target": "clarification",
                "required_clarification": "暂时无法可靠规划该请求，请稍后重试。",
                "assumption_draft": [],
            },
            rationale="intent plan 不可用",
        )

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
