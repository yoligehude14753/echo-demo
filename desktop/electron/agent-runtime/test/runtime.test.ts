import assert from "node:assert/strict";
import test from "node:test";
import { createRuntimeManifest, assertRuntimeManifestMatches } from "../worker/identity.ts";
import { WorkerManager, WorkerRuntimeError } from "../pool/worker-manager.ts";
import { WorkerPool } from "../pool/worker-pool.ts";
import type { AgentTurnInput, KernelBuildIdentity, OpenSessionInput } from "../../../agent-kernel/core/index.ts";

const identity: KernelBuildIdentity = {
  schemaVersion: 1,
  kernelApiVersion: 1,
  workerProtocolVersion: 1,
  modelSchemaVersion: 1,
  grantSchemaVersion: 1,
  checkpointSchemaVersion: 1,
  eventSchemaVersion: 1,
  buildId: "b04k-worker-test-v1",
  sourceSnapshotId: "sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a",
  sourceManifestSha256: "b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a",
  echoBaselineSha: "1904cb8c49502d64c53ff163d6e04b88d396c751",
  runtimeFingerprint: {
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
    napi: "10",
  },
};

const manifest = createRuntimeManifest(identity, "b04k-worker-test-manifest");
const factoryModule = new URL("./fixtures/kernel-runtime-fixture.ts", import.meta.url);
const workerEntry = new URL("../worker/worker-entry.ts", import.meta.url);

function openInput(taskId = "task-runtime", operationKey = "operation-runtime"): OpenSessionInput {
  return {
    taskId,
    operationKey,
    model: {
      schemaVersion: 1,
      revision: 1,
      configHash: "sha256:model",
      purpose: "agent_main",
      routeId: "fixture",
      protocol: "openai_chat",
      model: "fixture",
      capabilities: {
        streaming: true,
        toolUse: true,
        parallelToolUse: false,
        toolChoice: true,
        systemMessages: true,
        usageInStream: true,
        promptCache: false,
        multimodalImages: false,
        multimodalDocuments: false,
      },
      limits: { contextWindow: 4096, maxOutputTokens: 256, requestTimeoutSeconds: 30, maxRetries: 0 },
      tokenizer: { kind: "conservative_estimate", identifier: "fixture", estimated: true, safetyMarginTokens: 32 },
      reasoning: { mode: "none", stripThinkTags: false, tokenBudget: null },
      credentialHandle: "redacted:fixture",
    },
    grant: {
      schemaVersion: 1,
      grantId: "grant-runtime",
      revision: 1,
      taskId,
      deviceId: "device-runtime",
      issuedAt: "2026-07-15T00:00:00.000Z",
      expiresAt: "2099-07-15T00:00:00.000Z",
      workspaceRoots: [],
      command: { mode: "deny", allowedExecutables: [], deniedPatterns: [], maxWallSeconds: 1, maxOutputBytes: 1024 },
      network: { mode: "deny", hosts: [], schemes: [], ports: [], allowPrivateAddresses: false },
      artifacts: {},
      secrets: {},
      skills: {},
    },
    limits: {
      wallSeconds: 30,
      maxTurns: 4,
      maxToolCalls: 4,
      maxModelInputTokens: 4096,
      maxModelOutputTokens: 256,
      maxToolOutputBytes: 1024,
      maxArtifactBytes: 1024,
      maxConcurrentTools: 1,
    },
  };
}

function turn(input: OpenSessionInput, context: Record<string, boolean> = {}): AgentTurnInput {
  return {
    schemaVersion: 1,
    taskId: input.taskId,
    operationKey: input.operationKey,
    userMessage: "hello",
    systemPrompt: "system",
    outputContract: {},
    context,
    deadlineAt: "2099-07-15T00:00:00.000Z",
  };
}

function manager(): WorkerManager {
  return new WorkerManager({ manifest, factoryModule, workerEntry });
}

test("production worker turn and checkpoint stay on the same PID with v1 identity", async () => {
  const runtime = manager();
  const input = openInput();
  const session = await runtime.open(input);
  const events = [];
  for await (const event of session.runTurn(turn(input))) events.push(event);
  assert.deepEqual(events.map((event) => event.type), ["agent.turn.started", "agent.message.delta", "agent.message.completed", "agent.turn.completed"]);
  assert.equal(events.every((event) => event.runtimeEventId.startsWith("fixture-runtime-")), true);
  assert.equal(events[0]?.payload.workerPid, process.pid);
  assert.equal(typeof events[0]?.payload.workerThreadId, "number");
  assert.ok(Number(events[0]?.payload.workerThreadId) > 0);
  const checkpoint = await session.checkpoint();
  assert.equal(checkpoint.schemaVersion, 1);
  assert.equal(checkpoint.compactState.strategy, "none");
  assert.equal(checkpoint.lastDurableEventSeq, 0);
  await session.close();
  await runtime.close();
});

test("cancel is concurrent with an in-flight turn and remains idempotent", async () => {
  const runtime = manager();
  const input = openInput();
  const session = await runtime.open(input);
  const iterator = session.runTurn(turn(input, { waitForCancel: true }))[Symbol.asyncIterator]();
  const first = await iterator.next();
  assert.equal(first.value?.type, "agent.turn.started");
  await session.cancel("user");
  await session.cancel("user");
  const rest = [];
  for await (const event of { [Symbol.asyncIterator]: () => iterator }) rest.push(event);
  assert.deepEqual(rest.map((event) => event.type), ["agent.turn.cancelled"]);
  await session.close();
  await runtime.close();
});

test("worker crash fails the turn and restart opens the same task/operation", async () => {
  const runtime = manager();
  const input = openInput();
  const session = await runtime.open(input);
  await assert.rejects(
    (async () => {
      for await (const event of session.runTurn(turn(input, { crash: true }))) {
        // The fixture exits before it can emit a terminal event.
        void event;
      }
    })(),
    (error: unknown) => error instanceof WorkerRuntimeError && error.code === "RUNTIME_WORKER_CRASHED",
  );
  const restarted = await session.restart();
  const events = [];
  for await (const event of restarted.runTurn(turn(input))) events.push(event);
  assert.equal(events.at(-1)?.type, "agent.turn.completed");
  await restarted.close();
  await runtime.close();
});

test("pool leases one worker and releases it to the next waiter", async () => {
  const pool = new WorkerPool({ size: 1, manifest, factoryModule, workerEntry });
  const firstInput = openInput("task-one", "operation-one");
  const secondInput = openInput("task-two", "operation-two");
  const first = await pool.open(firstInput);
  assert.equal(pool.busyCount, 1);
  const secondPromise = pool.open(secondInput);
  await first.close();
  const second = await secondPromise;
  assert.equal(second.input.taskId, "task-two");
  await second.close();
  await pool.close();
});

test("manifest mismatch is rejected before worker session use", () => {
  const mismatched = createRuntimeManifest({ ...identity, buildId: "other-build" }, manifest.manifestId);
  assert.throws(() => assertRuntimeManifestMatches(manifest, mismatched), /identity mismatch/);
});
