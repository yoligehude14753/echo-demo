import type { IntentResult } from "@/types";

export interface IntentPlanGateDecision {
  allowDispatch: boolean;
  message: string;
  failed: boolean;
  serializedPlan: string | null;
  contextRefs: string[];
}

const TARGETS = new Set([
  "builtin_skill",
  "claude_code_runtime",
  "conversation",
  "clarification",
  // A rolling update can briefly pair a new renderer with an old backend
  // response. The backend canonicalizes this value on the next plan.
  "conversational_response",
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
  const contextRefs = Array.isArray(plan?.available_context)
    ? plan.available_context.filter(
        (item): item is string => typeof item === "string" && item.length > 0,
      )
    : [];
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
    return {
      allowDispatch: true,
      message: "",
      failed: false,
      serializedPlan: JSON.stringify(plan),
      contextRefs,
    };
  }
  return {
    allowDispatch: false,
    message: clarification || (failed
      ? "暂时无法可靠规划该请求，请稍后重试。"
      : "请补充必要信息后再执行。"),
    failed,
    serializedPlan: null,
    contextRefs: [],
  };
}
