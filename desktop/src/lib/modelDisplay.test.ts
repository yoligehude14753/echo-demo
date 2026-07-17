import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { modelDisplayName, PRODUCT_MODEL_NAME, SMALL_MODEL_NAME } from "./modelDisplay.ts";

test("uses product display names without exposing provider or route identifiers", () => {
  assert.equal(PRODUCT_MODEL_NAME, "DeepSeek V4 Flash");
  assert.equal(SMALL_MODEL_NAME, "Qwen3 8B");
  assert.equal(modelDisplayName("deepseek-v4-flash"), "DeepSeek V4 Flash");
  assert.equal(modelDisplayName("gpt-5.4-nano"), "Qwen3 8B");
  assert.equal(modelDisplayName("agent_fast_route_internal"), "Qwen3 8B");
  assert.equal(modelDisplayName("memory_association"), "Qwen3 8B");
  assert.equal(modelDisplayName(), "DeepSeek V4 Flash");
});
