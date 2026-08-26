import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { modelDisplayName } from "./modelDisplay.ts";

test("uses the runtime model id without a second mapping table", () => {
  assert.equal(modelDisplayName("qwen3.5-35b-a3b"), "qwen3.5-35b-a3b");
  assert.equal(modelDisplayName("future-model-v2"), "future-model-v2");
  assert.equal(modelDisplayName("  future-model-v2  "), "future-model-v2");
  assert.equal(modelDisplayName(), "模型未确认");
});
