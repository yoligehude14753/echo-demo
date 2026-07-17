import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { modelDisplayName, PRODUCT_MODEL_NAME } from "./modelDisplay.ts";

test("uses the product name without exposing runtime model or route identifiers", () => {
  assert.equal(PRODUCT_MODEL_NAME, "Qwen3-B8");
  assert.equal(modelDisplayName("gpt-5.4-nano"), "Qwen3-B8");
  assert.equal(modelDisplayName("agent_fast_route_internal"), "Qwen3-B8");
  assert.equal(modelDisplayName(), "Qwen3-B8");
});
