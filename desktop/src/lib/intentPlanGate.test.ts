import assert from "node:assert/strict";
import test from "node:test";

import type { IntentResult } from "../types";
// @ts-expect-error Node's strip-types runner executes the source test directly.
import { resolveIntentPlanGate } from "./intentPlanGate.ts";

function result(params: Record<string, unknown>): IntentResult {
  return { kind: "generate_pptx", confidence: 0.9, rationale: "test", params };
}

const readyPlan = {
  execution_target: "builtin_skill",
  missing_constraints: [],
  clarification_questions: [],
  confidence: 0.9,
  execution_authorized: true,
};

test("a backend-approved plan is the only dispatch authorization", () => {
  const decision = resolveIntentPlanGate(result({
    intent_plan: { ...readyPlan, available_context: ["可用资料：研究笔记.md"] },
    ready_to_execute: true,
  }));
  assert.equal(decision.allowDispatch, true);
  assert.deepEqual(decision.contextRefs, ["可用资料：研究笔记.md"]);
});

test("missing constraints and invalid plans fail closed", () => {
  const missing = resolveIntentPlanGate(result({
    intent_plan: { ...readyPlan, missing_constraints: ["受众"] },
    ready_to_execute: false,
    required_clarification: "请补充受众",
  }));
  assert.equal(missing.allowDispatch, false);
  assert.match(missing.message, /受众/);
  assert.equal(resolveIntentPlanGate(result({ plan_status: "failed", ready_to_execute: false })).failed, true);
});
