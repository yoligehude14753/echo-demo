import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { WorkerManager } from "../../pool/worker-manager.ts";
import { createRuntimeManifest } from "../../worker/identity.ts";
import { modelSnapshot } from "./production-worker-factory.mjs";

const runtimeRoot = new URL("../../", import.meta.url);
const contract = JSON.parse(readFileSync(new URL("./worker-contract.json", import.meta.url), "utf8"));
const runtimeRootPath = runtimeRoot.pathname;

function sourceFiles(root) {
  if (!existsSync(root)) return [];
  const files = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) files.push(...sourceFiles(path));
    else if (/\.(?:ts|mts|cts|mjs|cjs)$/.test(entry.name)) files.push(path);
  }
  return files;
}

test("worker contract is frozen at v1 and inherits the stated baselines", () => {
  assert.equal(contract.schema_version, 1);
  assert.equal(contract.contract, "echo-agent-worker/v1");
  assert.equal(contract.kernel_contract, "echo-agent-kernel/v1");
  assert.equal(contract.compatibility_baseline_sha, "492053c53441793c220f3b8e1dd231f1faea6e42");
  assert.equal(contract.f04_evidence_commit, "db57ddefc95c494c3785659db89befe6d8cf9c94");
  assert.equal(contract.input_base_sha, "1904cb8c49502d64c53ff163d6e04b88d396c751");
  assert.deepEqual(contract.contract_versions, {
    kernel_api: 1,
    worker_ipc: 1,
    model_runtime: 1,
    grant_snapshot: 1,
    checkpoint: 1,
    event: 1,
  });
});

test("production worker surface is present and has no kernel-side forbidden imports", () => {
  const directories = ["worker", "pool", "message-port"].map((directory) => join(runtimeRootPath, directory));
  const files = directories.flatMap(sourceFiles);
  assert.ok(files.length > 0, "BLOCKED_PRODUCTION_WORKER_MISSING: worker/pool/message-port source is absent");
  const forbidden = new RegExp(`(?:${contract.forbidden_imports.map((item) => item.replace(/[.*+?^${}()|[\\]\\]/g, "\\$&")).join("|")})`);
  for (const file of files) {
    assert.doesNotMatch(readFileSync(file, "utf8"), forbidden, file);
  }
});

test("production worker executes compact-summary-checkpoint and same-PID restart proof", async () => {
  const identity = {
    schemaVersion: 1,
    kernelApiVersion: 1,
    workerProtocolVersion: 1,
    modelSchemaVersion: 1,
    grantSchemaVersion: 1,
    checkpointSchemaVersion: 1,
    eventSchemaVersion: 1,
    buildId: "b04k-production-worker-v1",
    sourceSnapshotId: "sha256:" + "b".repeat(64),
    sourceManifestSha256: "a".repeat(64),
    echoBaselineSha: contract.compatibility_baseline_sha.slice(0, 40),
    runtimeFingerprint: {
      electron: contract.required_runtime.electron,
      node: contract.required_runtime.node,
      v8: "15.0.245.13-electron.0",
      modules: contract.required_runtime.modules,
      napi: "10",
    },
  };
  const grant = {
    schemaVersion: 1,
    grantId: "b04k-test-grant",
    revision: 1,
    taskId: "b04k-production-task",
    deviceId: "b04k-test-device",
    issuedAt: "2026-07-15T00:00:00.000Z",
    expiresAt: "2026-07-16T00:00:00.000Z",
    workspaceRoots: [],
    command: { mode: "deny", allowedExecutables: [], deniedPatterns: [], maxWallSeconds: 1, maxOutputBytes: 1024 },
    network: { mode: "deny", hosts: [], schemes: [], ports: [], allowPrivateAddresses: false },
    artifacts: {},
    secrets: {},
    skills: {},
  };
  const open = {
    taskId: grant.taskId,
    operationKey: "b04k-production-operation",
    model: modelSnapshot,
    grant,
    limits: {
      wallSeconds: 60,
      maxTurns: 4,
      maxToolCalls: 0,
      maxModelInputTokens: 4096,
      maxModelOutputTokens: 128,
      maxToolOutputBytes: 1024,
      maxArtifactBytes: 4096,
      maxConcurrentTools: 1,
    },
  };
  const turn = {
    schemaVersion: 1,
    taskId: open.taskId,
    operationKey: open.operationKey,
    userMessage: "hello",
    systemPrompt: "system",
    outputContract: {},
    context: {},
    deadlineAt: "2026-07-15T12:00:00.000Z",
  };
  const manager = new WorkerManager({
    manifest: createRuntimeManifest(identity, "b04k-production-manifest-v1"),
    factoryModule: new URL("./production-worker-factory.mjs", import.meta.url),
    startupTimeoutMs: 5000,
  });
  let session;
  try {
    session = await manager.open(open);
    const events = [];
    for await (const event of session.runTurn(turn)) events.push(event);
    const checkpoint = await session.checkpoint();
    assert.equal(events.at(-1)?.type, "agent.turn.completed");
    assert.ok(events.some((event) => event.type === "agent.compaction.completed"), "production turn did not compact");
    assert.ok(events.some((event) => event.type === "agent.summary.updated"), "production turn did not emit summary");
    assert.equal(checkpoint.schemaVersion, 1);
    assert.equal(checkpoint.taskId, open.taskId);
    assert.equal(checkpoint.operationKey, open.operationKey);
    assert.equal(checkpoint.compactState.strategy, "microcompact");
    const runtimeEvidence = events.find((event) => event.payload.workerPid !== undefined);
    assert.equal(runtimeEvidence?.payload.workerPid, process.pid);
    assert.ok(Number(runtimeEvidence?.payload.workerThreadId) > 0);
    const restarted = await session.restart();
    assert.equal(manager.currentState, "open");
    await restarted.close();
  } finally {
    await manager.close();
  }
});
