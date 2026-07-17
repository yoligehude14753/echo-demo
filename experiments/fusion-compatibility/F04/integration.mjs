import assert from "node:assert/strict";
import {
  AdapterError,
  DeterministicAdapterSession,
  ToolCorrelationLedger,
} from "./adapter/adapter.ts";

const SOURCE_SNAPSHOT =
  "sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";
const ECHO_BASELINE = "492053c53441793c220f3b8e1dd231f1faea6e42";
const SOURCE_MANIFEST = "b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";

const identities = {
  macos: {
    platform: "darwin",
    arch: "arm64",
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
  },
  sunny: {
    platform: "win32",
    arch: "x64",
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
  },
};

const modelSnapshot = {
  schemaVersion: 1,
  revision: 3,
  configHash: "sha256:f04-deterministic-model",
  purpose: "agent_main",
  routeId: "f04-fake-route",
  protocol: "anthropic_messages",
  model: "deterministic-fake-v1",
  capabilities: { streaming: true, toolUse: true, parallelToolUse: false },
  limits: { maxOutputTokens: 256 },
  credentialHandle: "redacted:f04",
};

const grant = {
  schemaVersion: 1,
  grantId: "grant-f04-001",
  revision: 7,
  taskId: "f04-task-integrated",
  expiresAt: "2026-01-01T00:30:00.000Z",
};

function identity(platform) {
  return {
    sourceSnapshotId: SOURCE_SNAPSHOT,
    sourceManifestSha256: SOURCE_MANIFEST,
    echoBaselineSha: ECHO_BASELINE,
    runtime: identities[platform],
  };
}

function input(operationKey = "op-integrated") {
  return {
    schemaVersion: 1,
    taskId: grant.taskId,
    operationKey,
    systemPrompt: "F04 deterministic system",
    userMessage: [{ type: "text", text: "Read demo.txt and summarize it" }],
    context: { grantId: grant.grantId, grantRevision: grant.revision },
    outputContract: { kind: "text" },
    deadlineAt: "2026-01-01T00:10:00.000Z",
    messages: [],
  };
}

function fakeTool() {
  let invocations = 0;
  return {
    get invocations() {
      return invocations;
    },
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
            return {
              toolUseId: call.toolUseId,
              output: { type: "text", text: "fixture contents" },
              isError: false,
            };
          },
        };
      },
    },
  };
}

function fakeModel() {
  return {
    snapshot: () => modelSnapshot,
    async *stream(request) {
      const continued = request.messages.some((message) =>
        message.content.some((content) => content.type === "tool_result"),
      );
      yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
      if (!continued) {
        yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "准备读取" };
        yield { schemaVersion: 1, type: "tool_start", requestId: request.requestId, index: 0, id: "tool-1", name: "EchoRead" };
        yield { schemaVersion: 1, type: "tool_arguments_delta", requestId: request.requestId, index: 0, json: '{"path":"demo.txt"}' };
        yield { schemaVersion: 1, type: "tool_stop", requestId: request.requestId, index: 0 };
        yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "tool_use" };
        return;
      }
      yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "已读取 demo.txt" };
      yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
    },
  };
}

function openSession(platform, actual = identity(platform), model = fakeModel(), tools = fakeTool().registry) {
  return new DeterministicAdapterSession({
    expectedIdentity: identity(platform),
    actualIdentity: actual,
    model,
    tools,
    grant,
    clock: () => "2026-01-01T00:00:00.000Z",
  });
}

async function success(platform) {
  const tool = fakeTool();
  const session = openSession(platform, identity(platform), fakeModel(), tool.registry);
  const events = await session.runTurn(input());
  assert.equal(events.at(-1).terminal.state, "succeeded");
  assert.equal(events.find((event) => event.type === "agent.tool.completed").payload.toolUseId, "tool-1");
  assert.equal(tool.invocations, 1);
  assert.ok(events.every((event) => event.schemaVersion === 1));
  return { events: events.length, invocations: tool.invocations, terminal: "succeeded" };
}

async function cancel(platform) {
  const blockingModel = {
    snapshot: () => modelSnapshot,
    async *stream(request, signal) {
      yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
      await new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    },
  };
  const session = openSession(platform, identity(platform), blockingModel, { resolve: () => undefined });
  const running = session.runTurn(input("op-integrated-cancel"));
  await new Promise((resolve) => setImmediate(resolve));
  session.cancel({ cancelRequestId: "cancel-integrated", reason: "user", requestedAt: "2026-01-01T00:01:00.000Z", expectedRevision: 7 });
  const events = await running;
  assert.equal(events.at(-1).terminal.state, "cancelled");
  return { events: events.length, invocations: 0, terminal: "cancelled" };
}

function failClosed(platform) {
  const ledger = new ToolCorrelationLedger();
  ledger.register("tool-1");
  assert.throws(
    () => ledger.accept({ toolUseId: "wrong-id", output: { type: "text", text: "bad" }, isError: false }),
    (error) => error instanceof AdapterError && error.code === "MODEL_TOOL_CORRELATION_MISMATCH" && error.details.toolInvoked === false,
  );
  assert.throws(
    () => openSession(platform, { ...identity(platform), sourceSnapshotId: "sha256:wrong" }),
    (error) => error instanceof AdapterError && error.code === "SOURCE_SNAPSHOT_MISMATCH",
  );
  assert.throws(
    () => openSession(platform, { ...identity(platform), runtime: { ...identity(platform).runtime, modules: "147" } }),
    (error) => error instanceof AdapterError && error.code === "RUNTIME_FINGERPRINT_MISMATCH",
  );
  return { correlation: "MODEL_TOOL_CORRELATION_MISMATCH", source: "SOURCE_SNAPSHOT_MISMATCH", runtime: "RUNTIME_FINGERPRINT_MISMATCH" };
}

const platform = process.argv[2] ?? "macos";
if (!identities[platform]) throw new Error(`platform must be macos or sunny: ${platform}`);
const result = {
  schema: "f04-integrated-deterministic-trace.v1",
  platform,
  sourceSnapshot: SOURCE_SNAPSHOT,
  echoBaseline: ECHO_BASELINE,
  success: await success(platform),
  cancel: await cancel(platform),
  failClosed: failClosed(platform),
};
console.log(JSON.stringify(result));
