import assert from "node:assert/strict";
import test from "node:test";

import type { IntentResult } from "../types";
// @ts-expect-error Node strip-types requires the explicit source extension.
import { requiresPptIntentPlan, resolvePptPlanGate } from "./pptIntentPlan.ts";

function result(params: Record<string, unknown>): IntentResult {
  return {
    kind: "generate_pptx",
    confidence: 0.9,
    rationale: "test",
    params,
  };
}

test("explicit PPT command must not use the local generate shortcut", () => {
  assert.equal(requiresPptIntentPlan("@生成 PPT 测试"), true);
  assert.equal(requiresPptIntentPlan("@生成 幻灯片 测试"), true);
  assert.equal(requiresPptIntentPlan("@生成 HTML 测试"), false);
});

test("ambiguous WorkBuddy PPT stays at clarification and cannot call artifact", () => {
  const decision = resolvePptPlanGate(
    result({
      ready_to_generate: false,
      plan_status: "clarification_required",
      required_clarification: "这份 PPT 给谁看、用于什么决策，并以哪些资料为准？",
      assumption_draft: ["可先按内部选型汇报草案组织"],
    }),
  );

  assert.equal(decision?.allowArtifact, false);
  assert.match(decision?.message ?? "", /给谁看/);
  assert.match(decision?.message ?? "", /可选假设草案/);
});

test("context-rich valid plan is the only PPT path allowed to call artifact", () => {
  const plan = {
    goal: "支持管理层完成 WorkBuddy 选型决策",
    audience: "管理层",
    deliverable: "pptx",
    available_context: ["当前会话：管理层软件选型评审", "可用资料：WorkBuddy访谈纪要.md"],
    missing_constraints: [],
    assumptions: [],
    outline: ["目标与范围", "产品能力", "竞争对比", "建议与下一步"],
    required_clarification: null,
    confidence: 0.92,
  };
  const decision = resolvePptPlanGate(
    result({
      ready_to_generate: true,
      plan_status: "ready",
      required_clarification: "",
      intent_plan: plan,
    }),
  );

  assert.equal(decision?.allowArtifact, true);
  assert.deepEqual(JSON.parse(decision?.serializedPlan ?? "{}"), plan);
});

test("invalid or failed plan fails closed", () => {
  const decision = resolvePptPlanGate(
    result({
      ready_to_generate: true,
      plan_status: "failed",
      required_clarification: "",
      intent_plan: { deliverable: "pptx" },
    }),
  );

  assert.equal(decision?.allowArtifact, false);
  assert.equal(decision?.failed, true);
  assert.match(decision?.message ?? "", /稍后重试/);
});
