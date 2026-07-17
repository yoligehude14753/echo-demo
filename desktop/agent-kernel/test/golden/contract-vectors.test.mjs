import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { checkpointChecksum, verifyCheckpointChecksum } from "../../core/checkpoint.ts";
import { KernelError } from "../../core/errors.ts";
import { assertSameBuildIdentity } from "../../core/identity.ts";
import { microCompactMessages } from "../../src/compact/microCompact.ts";
import {
  checkTokenBudget,
  createBudgetTracker,
} from "../../src/context/budget.ts";

globalThis.crypto ??= webcrypto;

const root = new URL("../../", import.meta.url);
const vectors = JSON.parse(readFileSync(new URL("./contract-vectors.json", import.meta.url), "utf8"));

const identity = {
  schemaVersion: 1,
  kernelApiVersion: 1,
  workerProtocolVersion: 1,
  modelSchemaVersion: 1,
  grantSchemaVersion: 1,
  checkpointSchemaVersion: 1,
  eventSchemaVersion: 1,
  buildId: "b04k-golden-v1",
  sourceSnapshotId: vectors.source_snapshot_id,
  sourceManifestSha256: vectors.manifest_root_sha256,
  echoBaselineSha: vectors.compatibility_baseline_sha.slice(0, 40),
  runtimeFingerprint: {
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
    napi: "10",
  },
};

test("contract vectors are frozen to B04K input and contract v1", () => {
  assert.equal(vectors.schema_version, 1);
  assert.equal(vectors.contract, "echo-agent-kernel/v1");
  assert.equal(vectors.compatibility_baseline_sha, "492053c53441793c220f3b8e1dd231f1faea6e42");
  assert.equal(vectors.f04_evidence_commit, "db57ddefc95c494c3785659db89befe6d8cf9c94");
  assert.equal(vectors.input_base_sha, "1904cb8c49502d64c53ff163d6e04b88d396c751");
  assert.deepEqual(vectors.contract_versions, {
    kernel_api: 1,
    worker_ipc: 1,
    model_runtime: 1,
    grant_snapshot: 1,
    checkpoint: 1,
    event: 1,
  });
  assert.deepEqual(vectors.excluded_source_gaps, [
    "services/compact/reactiveCompact.js",
    "services/contextCollapse/index.js",
    "services/compact/snipCompact.js",
    "utils/taskSummary.js",
  ]);
  assert.equal(vectors.golden_vectors.length, 3);
});

test("compact and budget golden vectors carry exact fail-closed expectations", () => {
  const budgetContinuation = vectors.golden_vectors.find((vector) => vector.id === "budget-continuation");
  const budgetStop = vectors.golden_vectors.find((vector) => vector.id === "budget-threshold-stop");
  const compact = vectors.golden_vectors.find((vector) => vector.id === "micro-compact-preserves-tool-correlation");
  const continuation = checkTokenBudget(
    createBudgetTracker(1000),
    budgetContinuation.input.budget,
    budgetContinuation.input.globalTurnTokens,
    budgetContinuation.input.now,
  );
  assert.equal(continuation.action, budgetContinuation.expected.action);
  assert.equal(continuation.continuationCount, budgetContinuation.expected.continuationCount);
  assert.equal(continuation.pct, budgetContinuation.expected.pct);
  assert.equal(continuation.turnTokens, budgetContinuation.expected.turnTokens);

  const stop = checkTokenBudget(
    createBudgetTracker(1000),
    budgetStop.input.budget,
    budgetStop.input.globalTurnTokens,
    budgetStop.input.now,
  );
  assert.deepEqual(stop, budgetStop.expected);

  const compacted = microCompactMessages(compact.input.messages, {
    keepRecent: compact.input.keepRecent,
  });
  assert.equal(compacted.changed, compact.expected.changed);
  assert.deepEqual(compacted.clearedToolUseIds, compact.expected.clearedToolUseIds);
  assert.equal(compacted.messages[1].content[0].result.content, compact.expected.clearedContent);
  assert.equal(compacted.tokensSaved, compact.expected.tokensSaved);
});

test("kernel core has no forbidden direct side-effect imports", () => {
  const forbidden = /(?:node:(?:fs|fs\/promises|child_process|net|http|https)|\bfrom\s+["']electron["'])/;
  const files = ["checkpoint.ts", "errors.ts", "identity.ts", "index.ts", "kernel.ts", "types.ts"];
  for (const file of files) {
    const source = readFileSync(new URL(`../../core/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, forbidden, file);
  }
});

test("corrupt checkpoint is rejected with CHECKPOINT_CORRUPT", async () => {
  const body = {
    schemaVersion: 1,
    checkpointId: "checkpoint-golden-1",
    taskId: "task-golden",
    operationKey: "operation-golden",
    modelConfigRevision: 7,
    grantRevision: 3,
    lastDurableEventSeq: 4,
    messages: [],
    compactState: { schemaVersion: 1, strategy: "none", summaryHash: null, messageCountAtBoundary: 0 },
    budgetState: { turnsUsed: 1, toolCallsUsed: 0, modelInputTokens: 4, modelOutputTokens: 2 },
    createdAt: "2026-07-15T00:00:00.000Z",
  };
  const checkpoint = { ...body, checksum: await checkpointChecksum(body) };
  await verifyCheckpointChecksum(checkpoint);
  const corrupt = { ...checkpoint, operationKey: "operation-tampered" };
  await assert.rejects(verifyCheckpointChecksum(corrupt), (error) => {
    assert.ok(error instanceof KernelError);
    assert.equal(error.code, "CHECKPOINT_CORRUPT");
    return true;
  });
});

test("manifest/build identity mismatch is rejected before runtime use", () => {
  const mismatched = { ...identity, sourceManifestSha256: "f".repeat(64) };
  assert.throws(() => assertSameBuildIdentity(identity, mismatched), (error) => {
    assert.ok(error instanceof KernelError);
    assert.equal(error.code, "RUNTIME_BUILD_MISMATCH");
    return true;
  });
});

test("golden verifier itself stays inside the task-owned verification root", () => {
  assert.equal(root.pathname.endsWith("/desktop/agent-kernel/"), true);
  assert.equal(join(root.pathname, "test/golden/contract-vectors.json").includes("desktop/agent-kernel/test/golden"), true);
});
