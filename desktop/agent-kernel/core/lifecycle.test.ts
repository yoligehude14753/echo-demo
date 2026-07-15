import assert from "node:assert/strict";
import { test } from "node:test";
import { EchoAgentKernel } from "./kernel.ts";
import { KernelError } from "./errors.ts";
import type {
  AgentModelEvent,
  AgentModelRequest,
  AgentResourceBudget,
  EchoAgentEventSink,
  EchoAgentTelemetryPort,
  EchoAgentSessionPort,
  EchoClock,
  EchoContextPort,
  EchoIdFactory,
  EchoModelPort,
  EchoToolRegistry,
  GrantSnapshot,
  KernelBuildIdentity,
  KernelDeps,
  KernelEventEnvelope,
  KernelAuditEntry,
  ModelRuntimeSnapshot,
} from "./types.ts";

const NOW = "2026-07-15T00:00:00.000Z";

const identity: KernelBuildIdentity = {
  schemaVersion: 1,
  kernelApiVersion: 1,
  workerProtocolVersion: 1,
  modelSchemaVersion: 1,
  grantSchemaVersion: 1,
  checkpointSchemaVersion: 1,
  eventSchemaVersion: 1,
  buildId: "kernel-test-v1",
  sourceSnapshotId: "sha256:" + "1".repeat(64),
  sourceManifestSha256: "0".repeat(64),
  echoBaselineSha: "2".repeat(40),
  runtimeFingerprint: {
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
    napi: "10",
  },
};

const modelSnapshot: ModelRuntimeSnapshot = {
  schemaVersion: 1,
  revision: 7,
  configHash: "config-test",
  purpose: "agent_main",
  routeId: "test-route",
  protocol: "openai_chat",
  model: "fake-model",
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
    maxOutputTokens: 256,
    requestTimeoutSeconds: 30,
    maxRetries: 0,
  },
  tokenizer: { kind: "conservative_estimate", identifier: "fake", estimated: true, safetyMarginTokens: 16 },
  reasoning: { mode: "none", stripThinkTags: false, tokenBudget: null },
  credentialHandle: "fake-handle",
};

const grant: GrantSnapshot = {
  schemaVersion: 1,
  grantId: "grant-test",
  revision: 3,
  taskId: "task-test",
  deviceId: "device-test",
  issuedAt: NOW,
  expiresAt: "2026-07-16T00:00:00.000Z",
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
  maxToolCalls: 4,
  maxModelInputTokens: 4096,
  maxModelOutputTokens: 128,
  maxToolOutputBytes: 1024,
  maxArtifactBytes: 4096,
  maxConcurrentTools: 1,
};

class FakeModel implements EchoModelPort {
  private readonly lateTerminal: boolean;

  constructor(lateTerminal: boolean) {
    this.lateTerminal = lateTerminal;
  }

  snapshot(): ModelRuntimeSnapshot {
    return modelSnapshot;
  }

  async countTokens(_request: { request: AgentModelRequest }): Promise<{ inputTokens: number; estimated: boolean }> {
    return { inputTokens: 4, estimated: true };
  }

  async *stream(request: AgentModelRequest, _signal: AbortSignal): AsyncIterable<AgentModelEvent> {
    yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
    yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "ok" };
    await Promise.resolve();
    yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
    if (this.lateTerminal) {
      yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
    }
  }
}

const emptyTools: EchoToolRegistry = {
  list: () => [],
  resolve: () => undefined,
};

const context: EchoContextPort = {
  async buildModelContext(_input, history) {
    return {
      system: [{ type: "text", text: "system" }],
      messages: [...history],
      tools: [],
    };
  },
};

class FakeIds implements EchoIdFactory {
  private sequence = 0;

  next(kind: "request" | "event" | "turn" | "message" | "checkpoint" | "cancel"): string {
    this.sequence += 1;
    return `${kind}-${this.sequence}`;
  }
}

class FakeSessionPort implements EchoAgentSessionPort {
  saved?: KernelEventEnvelope;
  checkpointSaved = false;
  closed = false;
  private readonly startupIdentity: KernelBuildIdentity;

  constructor(startupIdentity: KernelBuildIdentity = identity) {
    this.startupIdentity = startupIdentity;
  }

  async startup(_kernelIdentity: KernelBuildIdentity): Promise<KernelBuildIdentity> {
    return this.startupIdentity;
  }

  async currentDurableEventSeq(): Promise<number> {
    return 100;
  }

  async saveCheckpoint(_checkpoint: import("./types.ts").KernelCheckpoint): Promise<void> {
    this.checkpointSaved = true;
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

function deps(model: EchoModelPort, session = new FakeSessionPort()): KernelDeps & { sink: FakeSink; sessionPort: FakeSessionPort } {
  const sink = new FakeSink();
  const clock: EchoClock = { now: () => NOW };
  const telemetry: EchoAgentTelemetryPort = { record: async () => undefined };
  return {
    model,
    tools: emptyTools,
    session,
    sessionPort: session,
    events: sink,
    sink,
    context,
    clock,
    ids: new FakeIds(),
    telemetry,
  };
}

class FakeSink implements EchoAgentEventSink {
  readonly events: KernelEventEnvelope[] = [];
  readonly audits: KernelAuditEntry[] = [];

  async publish(event: KernelEventEnvelope): Promise<void> {
    this.events.push(event);
  }

  async audit(entry: KernelAuditEntry): Promise<void> {
    this.audits.push(entry);
  }
}

function input(): {
  taskId: string;
  operationKey: string;
  model: ModelRuntimeSnapshot;
  grant: GrantSnapshot;
  limits: AgentResourceBudget;
} {
  return { taskId: "task-test", operationKey: "operation-test", model: modelSnapshot, grant, limits };
}

function turnInput(): import("./types.ts").AgentTurnInput {
  return {
    schemaVersion: 1,
    taskId: "task-test",
    operationKey: "operation-test",
    userMessage: "hello",
    systemPrompt: "system",
    outputContract: {},
    context: {},
    deadlineAt: "2026-07-15T12:00:00.000Z",
  };
}

test("open/run/checkpoint/close is deterministic and close is idempotent", async () => {
  const kernel = new EchoAgentKernel(identity);
  const injected = deps(new FakeModel(false));
  const session = await kernel.openSession(input(), injected);
  const events = [];
  for await (const event of session.runTurn(turnInput())) events.push(event);
  assert.deepEqual(events.map((event) => event.type), [
    "agent.turn.started",
    "agent.message.delta",
    "agent.message.completed",
    "agent.turn.completed",
  ]);
  await session.cancel("user");
  const checkpoint = await session.checkpoint();
  assert.equal(checkpoint.taskId, "task-test");
  assert.equal(injected.sessionPort.checkpointSaved, true);
  await session.close();
  await session.close();
  assert.equal(injected.sessionPort.closed, true);
  await assert.rejects(session.checkpoint(), (error: unknown) => error instanceof KernelError && error.code === "KERNEL_SESSION_CLOSED");
});

test("cancel wins once and a late model terminal is audit-only", async () => {
  const kernel = new EchoAgentKernel(identity);
  const injected = deps(new FakeModel(true));
  const session = await kernel.openSession(input(), injected);
  const iterator = session.runTurn(turnInput())[Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value?.type, "agent.turn.started");
  assert.equal((await iterator.next()).value?.type, "agent.message.delta");
  await session.cancel("user");
  const remainder = [];
  for await (const event of { [Symbol.asyncIterator]: () => iterator }) remainder.push(event);
  assert.equal(remainder.at(-1)?.type, "agent.turn.cancelled");
  assert.equal(injected.sink.events.filter((event) => event.type.includes("turn.")).length, 2);
  assert.equal(injected.sink.audits.some((audit) => audit.kind === "late_terminal"), true);
  await session.cancel("user");
});

test("startup identity mismatch fails closed", async () => {
  const mismatched: KernelBuildIdentity = { ...identity, buildId: "other-build" };
  const injected = deps(new FakeModel(false), new FakeSessionPort(mismatched));
  const kernel = new EchoAgentKernel(identity);
  await assert.rejects(kernel.openSession(input(), injected), (error: unknown) => error instanceof KernelError && error.code === "RUNTIME_BUILD_MISMATCH");
});
