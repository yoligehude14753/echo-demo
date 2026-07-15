import assert from "node:assert/strict";
import { test } from "node:test";
import { WorkerManager } from "../pool/worker-manager.ts";
import { createRuntimeManifest } from "../worker/identity.ts";
import type { B13HostRequest } from "../bridge/b13-host-ipc.ts";
import type { AgentResourceBudget, AgentTurnInput, GrantSnapshot, JsonObject, KernelBuildIdentity, ModelRuntimeSnapshot, OpenSessionInput } from "../../../agent-kernel/core/index.ts";

const identity: KernelBuildIdentity = {
  schemaVersion: 1,
  kernelApiVersion: 1,
  workerProtocolVersion: 1,
  modelSchemaVersion: 1,
  grantSchemaVersion: 1,
  checkpointSchemaVersion: 1,
  eventSchemaVersion: 1,
  buildId: "b13-fused-host-ipc-v1",
  sourceSnapshotId: `sha256:${"b".repeat(64)}`,
  sourceManifestSha256: "a".repeat(64),
  echoBaselineSha: "8d5bdb6fdaa0b0d8e2be8275f98b4f6f862ccab5",
  runtimeFingerprint: {
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
    napi: "10",
  },
};

const model: ModelRuntimeSnapshot = {
  schemaVersion: 1,
  revision: 7,
  configHash: "c".repeat(64),
  purpose: "agent_main",
  routeId: "main",
  protocol: "openai_chat",
  model: "model-redacted",
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
  limits: {
    contextWindow: 8192,
    maxOutputTokens: 128,
    requestTimeoutSeconds: 30,
    maxRetries: 0,
  },
  tokenizer: { kind: "conservative_estimate", identifier: "b13", estimated: true, safetyMarginTokens: 8 },
  reasoning: { mode: "none", stripThinkTags: true, tokenBudget: null },
  credentialHandle: "credential://b13-test",
};

const grant: GrantSnapshot = {
  schemaVersion: 1,
  grantId: "grant-b13-fused",
  revision: 3,
  taskId: "task-b13-fused",
  deviceId: "device-b13",
  issuedAt: "2026-07-16T00:00:00.000Z",
  expiresAt: "2099-07-16T00:00:00.000Z",
  workspaceRoots: [],
  command: { mode: "deny", allowedExecutables: [], deniedPatterns: [], maxWallSeconds: 1, maxOutputBytes: 1024 },
  network: { mode: "deny", hosts: [], schemes: [], ports: [], allowPrivateAddresses: false },
  artifacts: {},
  secrets: {},
  skills: {},
};

const limits: AgentResourceBudget = {
  wallSeconds: 60,
  maxTurns: 4,
  maxToolCalls: 1,
  maxModelInputTokens: 4096,
  maxModelOutputTokens: 128,
  maxToolOutputBytes: 4096,
  maxArtifactBytes: 4096,
  maxConcurrentTools: 1,
};

function event(requestId: string, type: string, payload: JsonObject = {}): JsonObject {
  return { schemaVersion: 1, requestId, type, ...payload };
}

test("B13 worker IPC fuses model, B06P receipt, durable checkpoint, restart/resume", async () => {
  let durableEventSeq = 0;
  let checkpoints: Record<string, unknown>[] = [];
  const modelCalls = new Map<string, number>();
  const hostRequest = async (request: B13HostRequest): Promise<JsonObject> => {
    switch (request.method) {
      case "session.bind":
        return {
          tools: [{
            name: "path.read",
            description: "Read through B06P",
            inputSchema: { type: "object", properties: { path: { type: "string" }, rootId: { type: "string" } }, required: ["path", "rootId"] },
            traits: { readOnly: true, destructive: false, concurrencySafe: true, capability: "path.read" },
          }],
        };
      case "session.startup":
        return { kernelIdentity: request.payload.kernelIdentity as JsonObject };
      case "session.current_durable_event_seq":
        return { durableEventSeq };
      case "session.save_checkpoint":
        checkpoints = [...checkpoints, request.payload.checkpoint as Record<string, unknown>];
        return {};
      case "session.close":
        return {};
      case "model.count_tokens":
        return { inputTokens: 1, estimated: true };
      case "model.stream": {
        const rawRequest = request.payload.request as { requestId: string; messages: unknown[] };
        const count = (modelCalls.get(rawRequest.requestId) ?? 0) + 1;
        modelCalls.set(rawRequest.requestId, count);
        const hasToolResult = JSON.stringify(rawRequest.messages).includes("tool_result");
        if (!hasToolResult && count === 1) {
          return {
            events: [
              event(rawRequest.requestId, "message_start"),
              event(rawRequest.requestId, "tool_start", { index: 0, id: "tool-b13-1", name: "path.read" }),
              event(rawRequest.requestId, "tool_arguments_delta", { index: 0, json: JSON.stringify({ path: "fixture.txt", rootId: "root-b13" }) }),
              event(rawRequest.requestId, "tool_stop", { index: 0 }),
              event(rawRequest.requestId, "usage", { inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, estimated: true }),
              event(rawRequest.requestId, "message_stop", { stopReason: "tool_use" }),
            ],
          };
        }
        return {
          events: [
            event(rawRequest.requestId, "message_start"),
            event(rawRequest.requestId, "text_delta", { text: "fused" }),
            event(rawRequest.requestId, "usage", { inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, estimated: true }),
            event(rawRequest.requestId, "message_stop", { stopReason: "end_turn" }),
          ],
        };
      }
      case "tool.describe":
        return { description: "Read through B06P" };
      case "tool.validate":
        return { allowed: true };
      case "tool.invoke":
        return {
          value: "host-value",
          result: "host-value",
          isError: false,
          receipt: {
            schemaVersion: 1,
            receiptId: "receipt-b13-1",
            operation: "path.read",
            outcome: "allow",
            result: "succeeded",
            code: "ALLOWED",
            capability: "path.read",
            taskId: request.taskId,
            operationKey: request.operationKey,
            toolUseId: "tool-b13-1",
            grantId: grant.grantId,
            grantRevision: grant.revision,
            policyRevision: 4,
            workspaceId: "ws-b13",
            redacted: true,
          },
        };
      case "events.publish":
        durableEventSeq += 1;
        return { durableEventSeq };
      case "events.audit":
      case "telemetry.record":
        return {};
      default:
        throw new Error(`unsupported host method ${request.method}`);
    }
  };

  const open: OpenSessionInput = {
    taskId: grant.taskId,
    operationKey: "operation-b13-fused",
    model,
    grant,
    limits,
  };
  const turn: AgentTurnInput = {
    schemaVersion: 1,
    taskId: open.taskId,
    operationKey: open.operationKey,
    userMessage: "hello",
    systemPrompt: "system",
    outputContract: {},
    context: {},
    deadlineAt: "2099-07-15T00:00:00.000Z",
  };
  const manager = new WorkerManager({
    manifest: createRuntimeManifest(identity, "b13-fused-manifest"),
    factoryModule: new URL("../bridge/b13-worker-factory.ts", import.meta.url),
    factoryData: { schemaVersion: 1, depsModule: new URL("../bridge/b13-host-kernel-deps.ts", import.meta.url).toString() },
    hostRequest,
    startupTimeoutMs: 5000,
  });
  try {
    const session = await manager.open(open);
    const events = [];
    for await (const item of session.runTurn(turn)) events.push(item);
    assert.equal(events.at(-1)?.type, "agent.turn.completed");
    const toolCompleted = events.find((item) => item.type === "agent.tool.completed");
    assert.equal((toolCompleted?.payload.receipt as { receiptId?: unknown } | undefined)?.receiptId, "receipt-b13-1");
    assert.equal(toolCompleted?.taskId, open.taskId);
    assert.equal(toolCompleted?.operationKey, open.operationKey);
    const checkpoint = await session.checkpoint();
    assert.equal(checkpoint.taskId, open.taskId);
    assert.equal(checkpoint.operationKey, open.operationKey);
    assert.ok(durableEventSeq > 0);
    assert.equal(checkpoints.at(-1)?.taskId, open.taskId);
    await session.close();
    const resumed = await manager.restart({ ...open, resume: checkpoint });
    assert.equal(resumed.input.taskId, open.taskId);
    assert.equal(resumed.input.operationKey, open.operationKey);
    await resumed.close();
  } finally {
    await manager.close();
  }
});
