import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { runGate } from "../../../scripts/agent-kernel/source-closure.mjs";

const sourceDir = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(sourceDir, "source-closure.fixture.json"), "utf8"));
const manifest = JSON.parse(readFileSync(join(sourceDir, "source-closure-manifest.json"), "utf8"));
const graph = JSON.parse(readFileSync(join(sourceDir, "source-import-graph.json"), "utf8"));
const gateSource = readFileSync(join(sourceDir, "../../../scripts/agent-kernel/source-closure.mjs"), "utf8");

test("source closure gate accepts the frozen, explicitly classified evidence closure", () => {
  const result = runGate();
  assert.equal(result.status, fixture.expected_gate_status);
  assert.deepEqual(graph.roots.map((root) => root.path), fixture.roots);
  for (const [key, expected] of Object.entries(fixture.expected_f01_counts)) {
    assert.equal(graph.f01_counts[key], expected);
  }
  for (const decision of fixture.required_decisions) {
    assert.ok(manifest.policy.allowed_decisions.includes(decision));
  }
  for (const exclusion of fixture.required_exclusions) {
    assert.ok(manifest.policy.forbidden_surface.includes(exclusion));
  }
});

test("source closure gate has no environment, network, launcher, or Claude execution path", () => {
  assert.doesNotMatch(gateSource, /process\.env\.(HOME|PATH)/);
  assert.doesNotMatch(gateSource, /node:(?:http|https|net|child_process)/);
  assert.doesNotMatch(gateSource, /\bfetch\s*\(/);
  assert.doesNotMatch(gateSource, /\b(?:spawn|exec)\s*\(/);
  assert.doesNotMatch(gateSource, /Claude\s+(?:run|launch|start)/i);
});
