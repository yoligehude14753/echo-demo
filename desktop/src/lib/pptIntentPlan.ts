import type { IntentResult } from "@/types";

export interface PptPlanGateDecision {
  allowArtifact: boolean;
  message: string;
  failed: boolean;
  serializedPlan: string | null;
}

interface PptIntentPlanShape {
  goal: string;
  audience: string;
  deliverable: "pptx";
  available_context: string[];
  missing_constraints: string[];
  assumptions: string[];
  outline: string[];
  required_clarification: string | null;
  confidence: number;
}

export function requiresPptIntentPlan(value: string): boolean {
  return /@\s*生成\s*(pptx?|幻灯片)/i.test(value);
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function validReadyPlan(value: unknown): value is PptIntentPlanShape {
  if (!value || typeof value !== "object") return false;
  const plan = value as Record<string, unknown>;
  return (
    typeof plan.goal === "string" &&
    plan.goal.trim().length > 0 &&
    typeof plan.audience === "string" &&
    plan.audience.trim().length > 0 &&
    plan.deliverable === "pptx" &&
    stringArray(plan.available_context) &&
    stringArray(plan.missing_constraints) &&
    plan.missing_constraints.length === 0 &&
    stringArray(plan.assumptions) &&
    stringArray(plan.outline) &&
    plan.outline.length > 0 &&
    (plan.required_clarification === null || plan.required_clarification === "") &&
    typeof plan.confidence === "number" &&
    plan.confidence >= 0.7 &&
    plan.confidence <= 1
  );
}

export function resolvePptPlanGate(result: IntentResult): PptPlanGateDecision | null {
  if (result.kind !== "generate_pptx") return null;
  const plan = result.params.intent_plan;
  const clarification =
    typeof result.params.required_clarification === "string"
      ? result.params.required_clarification.trim()
      : "";
  const allowArtifact =
    result.params.ready_to_generate === true &&
    result.params.plan_status === "ready" &&
    clarification.length === 0 &&
    validReadyPlan(plan);
  if (allowArtifact) {
    return {
      allowArtifact: true,
      message: "",
      failed: false,
      serializedPlan: JSON.stringify(plan),
    };
  }

  const assumptions = stringArray(result.params.assumption_draft)
    ? result.params.assumption_draft.filter((item) => item.trim().length > 0)
    : [];
  const fallback =
    result.params.plan_status === "failed"
      ? "暂时无法可靠规划这份 PPT，请稍后重试。"
      : "开始制作前，请补充这份 PPT 的目标、受众和资料范围。";
  return {
    allowArtifact: false,
    message:
      assumptions.length > 0
        ? `${clarification || fallback}\n\n可选假设草案：\n${assumptions
            .map((item) => `- ${item}`)
            .join("\n")}`
        : clarification || fallback,
    failed: result.params.plan_status === "failed",
    serializedPlan: null,
  };
}
