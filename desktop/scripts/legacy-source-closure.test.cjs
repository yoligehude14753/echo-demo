const assert = require("node:assert/strict");
const test = require("node:test");
const { scanLegacySourceClosure } = require("./legacy-source-closure.cjs");

test("legacy source closure excludes fused CoreShell assets while retaining old app contracts", () => {
  const result = scanLegacySourceClosure();
  assert.equal(result.status, "PASS");
  assert.equal(result.failure_count, 0);
});
