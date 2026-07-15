import assert from "node:assert/strict";
import test from "node:test";
import {
  AdapterError,
  DeterministicAdapterSession,
  ToolCorrelationLedger,
} from "./adapter.ts";

const identity = {
  sourceSnapshotId: "sha256:source-f04",
  sourceManifestSha256: "a".repeat(64),
  echoBaselineSha: "492053c53441793c220f3b8e1dd231f1faea6e42",
  runtime: {
    platform: "darwin",
    arch: "arm64",
    electron: "43.1.0",
    node: "22.15.0",
    v8: "12.4.254.21-electron.0",
    modules: "138",
  },
};

const grant = {
  schemaVersion: 1,
  grantId: "grant-f04",
  revision: 7,
  taskId: "task-f04",
  expiresAt: "2026-01-01T00:30:00.000Z",
};

const snapshot = {
  schemaVersion: 1,
  revision: 3,
  configHash: "sha256:model-f04",
  purpose: "agent_main",
  routeId: "fake-route",
  protocol: "anthropic_messages",
  model: "deterministic-fake",
  capabilities: { streaming: true, toolUse: true, parallelToolUse: false },
  limits: { maxOutputTokens: 256 },
  credentialHandle: "redacted:f04",
};

function input(operationKey = "op-tool") {
  return {
    schemaVersion: 1,
    taskId: "task-f04",
    operationKey,
    systemPrompt: "system",
    userMessage: [{ type: "text", text: "read the fixture" }],
    context: { grantExpiresAt: grant.expiresAt },
    outputContract: { kind: "text" },
    deadlineAt: "2026-01-01T00:10:00.000Z",
    messages: [],
  };
}

function fakeTool() {
  let invocations = 0;
  return {
    get invocations() { return invocations; },
    registry: {
      resolve(name) {
        if (name !== "EchoRead") return undefined;
        return {
          name,
          concurrencySafe: true,
          async invoke(call, context, signal) {
            assert.equal(context.grantId, grant.grantId);
            assert.equal(context.grantRevision, grant.revision);
            assert.equal(signal.aborted, false);
            invocations += 1;
            return { toolUseId: call.toolUseId, output: { type: "text", text: "fixture" }, isError: false };
          },
        };
      },
    },
  };
}

function toolModel() {
  return {
    snapshot: () => snapshot,
    async *stream(request) {
      const continued = request.messages.some((message) => message.content.some((content) => content.type === "tool_result"));
      if (!continued) {
        yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
        yield { schemaVersion: 1, type: "tool_start", requestId: request.requestId, index: 0, id: "tool-1", name: "EchoRead" };
        yield { schemaVersion: 1, type: "tool_arguments_delta", requestId: request.requestId, index: 0, json: '{"path":"fixture.txt"}' };
        yield { schemaVersion: 1, type: "tool_stop", requestId: request.requestId, index: 0 };
        yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "tool_use" };
        return;
      }
      yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
      yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "已读取" };
      yield { schemaVersion: 1, type: "usage", requestId: request.requestId, inputTokens: 10, outputTokens: 2, estimated: true };
      yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
    },
  };
}

function session(model, tools, actual = identity) {
  return new DeterministicAdapterSession({
    expectedIdentity: identity,
    actualIdentity: actual,
    model,
    tools,
    grant,
    clock: () => "2026-01-01T00:00:00.000Z",
  });
}

test("success/tool continuation preserves request and tool-use correlation", async () => {
  const tool = fakeTool();
  const events = await session(toolModel(), tool.registry).runTurn(input());
  assert.deepEqual(events.map((event) => event.seq), [1, 2, 3, 4, 5, 6]);
  assert.equal(events.at(-1).type, "agent.turn.completed");
  assert.equal(events.at(-1).terminal.state, "succeeded");
  assert.equal(events.find((event) => event.type === "agent.tool.completed").payload.toolUseId, "tool-1");
  assert.equal(events.find((event) => event.type === "agent.message.delta").payload.text, "已读取");
  assert.equal(tool.invocations, 1);
});

test("unknown or duplicate result fails closed before a tool invocation", () => {
  const ledger = new ToolCorrelationLedger();
  let invoked = 0;
  ledger.register("tool-1");
  assert.throws(
    () => ledger.accept({ toolUseId: "wrong-id", output: { type: "text", text: "bad" }, isError: false }),
    (error) => error instanceof AdapterError && error.code === "MODEL_TOOL_CORRELATION_MISMATCH" && error.details.toolInvoked === false,
  );
  assert.equal(invoked, 0);
});

test("model event schema mismatch becomes a typed terminal failure", async () => {
  const badModel = {
    snapshot: () => snapshot,
    async *stream(request) {
      yield { schemaVersion: 2, type: "message_start", requestId: request.requestId };
    },
  };
  const events = await session(badModel, { resolve: () => undefined }).runTurn(input("op-schema"));
  assert.equal(events.at(-1).type, "agent.turn.failed");
  assert.equal(events.at(-1).payload.code, "MODEL_SCHEMA_VERSION_MISMATCH");
  assert.equal(events.at(-1).terminal.state, "failed");
});

test("source/runtime mismatch rejects session startup", () => {
  const mismatched = { ...identity, sourceSnapshotId: "sha256:other" };
  assert.throws(
    () => session(toolModel(), { resolve: () => undefined }, mismatched),
    (error) => error instanceof AdapterError && error.code === "SOURCE_SNAPSHOT_MISMATCH",
  );
  const runtimeMismatch = { ...identity, runtime: { ...identity.runtime, node: "24.3.0" } };
  assert.throws(
    () => session(toolModel(), { resolve: () => undefined }, runtimeMismatch),
    (error) => error instanceof AdapterError && error.code === "RUNTIME_FINGERPRINT_MISMATCH",
  );
});

test("cancel is idempotent and first terminal wins", async () => {
  const blockingModel = {
    snapshot: () => snapshot,
    async *stream(request, signal) {
      yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
      await new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    },
  };
  const active = session(blockingModel, { resolve: () => undefined });
  const running = active.runTurn(input("op-cancel"));
  await new Promise((resolve) => setImmediate(resolve));
  active.cancel({ cancelRequestId: "cancel-1", reason: "user", requestedAt: "2026-01-01T00:01:00.000Z", expectedRevision: 7 });
  active.cancel({ cancelRequestId: "cancel-2", reason: "user", requestedAt: "2026-01-01T00:01:01.000Z", expectedRevision: 7 });
  const events = await running;
  assert.equal(events.filter((event) => event.terminal).length, 1);
  assert.equal(events.at(-1).type, "agent.turn.cancelled");
  assert.equal(events.at(-1).payload.cancelRequestId, "cancel-1");
});
