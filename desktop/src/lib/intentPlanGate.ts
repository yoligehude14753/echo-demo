import type { IntentResult } from "@/types";

export interface IntentPlanGateDecision {
  allowDispatch: boolean;
  message: string;
  failed: boolean;
  serializedPlan: string | null;
}

const TARGETS = new Set([
  "builtin_skill",
  "claude_code_runtime",
  "conversational_response",
  "clarification",
]);

/**
 * The UI treats every action as denied until the backend main-model plan proves
 * it is ready. This is deliberately generic: @ syntax and artifact keywords
 * never grant a local dispatch permission.
 */
export function resolveIntentPlanGate(result: IntentResult): IntentPlanGateDecision {
  const raw = result.params.intent_plan;
  const plan = raw && typeof raw === "object" && !Array.isArray(raw)
    ? raw as Record<string, unknown>
    : null;
  const failed = result.params.plan_status === "failed";
  const clarification = typeof result.params.required_clarification === "string"
    ? result.params.required_clarification.trim()
    : "";
  const target = plan?.execution_target;
  const missing = plan?.missing_constraints;
  const questions = plan?.clarification_questions;
  const confidence = plan?.confidence;
  const authorized = plan?.execution_authorized;
  const ready = result.params.ready_to_execute === true;
  const valid = Boolean(
    plan
      && typeof target === "string"
      && TARGETS.has(target)
      && Array.isArray(missing)
      && missing.length === 0
      && Array.isArray(questions)
      && questions.length === 0
      && typeof confidence === "number"
      && confidence >= 0.7
      && confidence <= 1
      && authorized === true
      && target !== "clarification",
  );
  if (ready && valid) {
    return { allowDispatch: true, message: "", failed: false, serializedPlan: JSON.stringify(plan) };
  }
  return {
    allowDispatch: false,
    message: clarification || (failed
      ? "暂时无法可靠规划该请求，请稍后重试。"
      : "请补充必要信息后再执行。"),
    failed,
    serializedPlan: null,
  };
}
