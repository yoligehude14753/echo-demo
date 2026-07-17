import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { modelDisplayName, PRODUCT_MODEL_NAME, SMALL_MODEL_NAME } from "./modelDisplay.ts";

test("keeps provider names hidden while preserving main and small model labels", () => {
  assert.equal(PRODUCT_MODEL_NAME, "DeepSeek V4 Flash");
  assert.equal(SMALL_MODEL_NAME, "Qwen3 8B");
  assert.equal(modelDisplayName("deepseek-v4-flash"), "DeepSeek V4 Flash");
  assert.equal(modelDisplayName("yunwu_llm_main"), "DeepSeek V4 Flash");
  assert.equal(modelDisplayName("qwen3-8b"), "Qwen3 8B");
  assert.equal(modelDisplayName("gpt-5.4-nano"), "Qwen3 8B");
  assert.equal(modelDisplayName("agent_fast_route_internal"), "Qwen3 8B");
  assert.equal(modelDisplayName(), "DeepSeek V4 Flash");
});
