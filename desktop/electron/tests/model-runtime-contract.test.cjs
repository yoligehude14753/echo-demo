"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createModelRuntimeIpcSurface,
  ModelRuntimeContractError,
  validateModelRuntimeFallback,
  validateModelRuntimeIdentity,
} = require("../model-runtime-contract.cjs");

const IDENTITY = {
  schemaVersion: 1,
  purpose: "agent_main",
  revision: 7,
  configHash: "8a5d7c3a5cf6a4d7d9e7a3f9bd4f2d0b9e5b0c5c1a4d2e3f6b7c8d9e0f1a2b34",
  routeId: "anthropic-primary",
  protocol: "anthropic_messages",
  model: "claude-sonnet-redacted",
};

const FALLBACK = {
  schemaVersion: 1,
  type: "agent.model.fallback",
  taskId: "task-settings-contract",
  operationKey: "op-settings-contract",
  configRevision: 7,
  fromRouteId: "anthropic-primary",
  toRouteId: "openai-compatible-primary",
  reason: "provider_timeout",
  occurredAt: "2026-07-15T00:00:00.000Z",
};

function rejects(value, code) {
  assert.throws(
    () => validateModelRuntimeIdentity(value),
    (error) => error instanceof ModelRuntimeContractError && error.code === code,
  );
}

test("model identity projection is strict, immutable, and round-trips without secrets", () => {
  const parsed = validateModelRuntimeIdentity({ ...IDENTITY });
  assert.deepEqual(parsed, IDENTITY);
  assert.equal(Object.isFrozen(parsed), true);
  assert.equal("credentialHandle" in parsed, false);
  assert.equal("baseUrl" in parsed, false);
});
test("missing, unknown, and mismatched identity fields fail closed", () => {
  const missing = { ...IDENTITY };
  delete missing.routeId;
  rejects(missing, "MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH");

  rejects({ ...IDENTITY, unexpected: true }, "MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH");
  rejects({ ...IDENTITY, schemaVersion: 2 }, "MODEL_SCHEMA_VERSION_MISMATCH");
  rejects({ ...IDENTITY, credentialHandle: "cred://secret" }, "MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH");
});

test("fallback event is explicit and remains visible after strict validation", () => {
  const parsed = validateModelRuntimeFallback({ ...FALLBACK });
  assert.deepEqual(parsed, FALLBACK);
  assert.equal(parsed.type, "agent.model.fallback");
  assert.equal(parsed.fromRouteId, "anthropic-primary");
  assert.equal(parsed.toRouteId, "openai-compatible-primary");
  assert.throws(
    () => validateModelRuntimeFallback({ ...FALLBACK, unknown: "field" }),
    (error) => error.code === "MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH",
  );
  assert.throws(
    () => validateModelRuntimeFallback({ ...FALLBACK, fromRouteId: FALLBACK.toRouteId }),
    (error) => error.code === "MODEL_RUNTIME_CONTRACT_INVALID",
  );
});

test("IPC identity read is trusted-origin guarded and renderer cannot publish", async () => {
  const handlers = new Map();
  const sent = [];
  const ipcMain = {
    handle(channel, handler) {
      handlers.set(channel, handler);
    },
  };
  const guardedEvents = [];
  const surface = createModelRuntimeIpcSurface({
    ipcMain,
    assertTrustedIpcOrigin(event) {
      guardedEvents.push(event);
    },
    sendToRenderers(channel, payload) {
      sent.push({ channel, payload });
    },
  });
  surface.register();
  assert.equal(handlers.has("model-runtime:get-identity"), true);
  assert.equal(await handlers.get("model-runtime:get-identity")({ source: "renderer" }), null);
  assert.deepEqual(guardedEvents, [{ source: "renderer" }]);

  surface.publishIdentity(IDENTITY);
  surface.publishFallback(FALLBACK);
  assert.deepEqual(sent, [
    { channel: "model-runtime:identity", payload: IDENTITY },
    { channel: "model-runtime:fallback", payload: FALLBACK },
  ]);
  assert.deepEqual(
    await handlers.get("model-runtime:get-identity")({ source: "renderer" }),
    IDENTITY,
  );
});
