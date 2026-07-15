"use strict";

const MODEL_RUNTIME_SCHEMA_VERSION = 1;

const IDENTITY_KEYS = Object.freeze([
  "schemaVersion",
  "purpose",
  "revision",
  "configHash",
  "routeId",
  "protocol",
  "model",
]);

const FALLBACK_KEYS = Object.freeze([
  "schemaVersion",
  "type",
  "taskId",
  "operationKey",
  "configRevision",
  "fromRouteId",
  "toRouteId",
  "reason",
  "occurredAt",
]);

const PURPOSES = new Set([
  "agent_main",
  "agent_compact",
  "agent_summary",
  "agent_quality",
  "chat",
  "minutes",
  "memory",
]);
const PROTOCOLS = new Set(["openai_chat", "anthropic_messages"]);

class ModelRuntimeContractError extends Error {
  constructor(code, message = "model runtime contract rejected") {
    super(message);
    this.name = "ModelRuntimeContractError";
    this.code = code;
  }
}

function reject(code, message) {
  throw new ModelRuntimeContractError(code, message);
}

function assertRecord(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", `${label} must be an object`);
  }
}

function assertExactKeys(value, expected, label) {
  const expectedSet = new Set(expected);
  const unknown = Object.keys(value).filter((key) => !expectedSet.has(key));
  const missing = expected.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    reject(
      "MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH",
      `${label} contains unknown or missing fields`,
    );
  }
}

function assertNonEmptyString(value, field) {
  if (typeof value !== "string" || value.trim() === "") {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", `${field} must be non-empty`);
  }
  return value;
}

function assertPositiveInteger(value, field) {
  if (!Number.isSafeInteger(value) || value < 1) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", `${field} must be a positive integer`);
  }
  return value;
}

function validateModelRuntimeIdentity(value) {
  assertRecord(value, "model runtime identity");
  assertExactKeys(value, IDENTITY_KEYS, "model runtime identity");
  if (value.schemaVersion !== MODEL_RUNTIME_SCHEMA_VERSION) {
    reject("MODEL_SCHEMA_VERSION_MISMATCH", "model runtime identity schema is unsupported");
  }
  if (!PURPOSES.has(value.purpose)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime identity purpose is invalid");
  }
  if (!PROTOCOLS.has(value.protocol)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime identity protocol is invalid");
  }
  assertPositiveInteger(value.revision, "revision");
  assertNonEmptyString(value.routeId, "routeId");
  assertNonEmptyString(value.model, "model");
  if (typeof value.configHash !== "string" || !/^[0-9a-f]{64}$/.test(value.configHash)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "configHash must be a lowercase sha256");
  }
  if (Object.prototype.hasOwnProperty.call(value, "credentialHandle")) {
    reject("MODEL_RUNTIME_SECRET_EXPOSURE", "credentialHandle is not a UI identity field");
  }
  return Object.freeze({ ...value });
}

function validateModelRuntimeFallback(value) {
  assertRecord(value, "model runtime fallback");
  assertExactKeys(value, FALLBACK_KEYS, "model runtime fallback");
  if (value.schemaVersion !== MODEL_RUNTIME_SCHEMA_VERSION) {
    reject("MODEL_SCHEMA_VERSION_MISMATCH", "model runtime fallback schema is unsupported");
  }
  if (value.type !== "agent.model.fallback") {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime fallback type is invalid");
  }
  for (const field of ["taskId", "operationKey", "fromRouteId", "toRouteId", "reason", "occurredAt"]) {
    assertNonEmptyString(value[field], field);
  }
  assertPositiveInteger(value.configRevision, "configRevision");
  if (value.fromRouteId === value.toRouteId) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "fallback routes must differ");
  }
  return Object.freeze({ ...value });
}

function createModelRuntimeIpcSurface({ ipcMain, assertTrustedIpcOrigin, sendToRenderers }) {
  if (!ipcMain || typeof ipcMain.handle !== "function") {
    throw new TypeError("ipcMain.handle is required");
  }
  if (typeof assertTrustedIpcOrigin !== "function") {
    throw new TypeError("trusted IPC origin guard is required");
  }
  if (typeof sendToRenderers !== "function") {
    throw new TypeError("renderer sender is required");
  }

  let identity = null;

  function register() {
    ipcMain.handle("model-runtime:get-identity", (event) => {
      assertTrustedIpcOrigin(event);
      return identity;
    });
  }

  function publishIdentity(value) {
    identity = validateModelRuntimeIdentity(value);
    sendToRenderers("model-runtime:identity", identity);
    return identity;
  }

  function publishFallback(value) {
    const fallback = validateModelRuntimeFallback(value);
    sendToRenderers("model-runtime:fallback", fallback);
    return fallback;
  }

  return Object.freeze({
    register,
    publishIdentity,
    publishFallback,
    validateModelRuntimeIdentity,
    validateModelRuntimeFallback,
  });
}

module.exports = {
  FALLBACK_KEYS,
  IDENTITY_KEYS,
  MODEL_RUNTIME_SCHEMA_VERSION,
  ModelRuntimeContractError,
  createModelRuntimeIpcSurface,
  validateModelRuntimeFallback,
  validateModelRuntimeIdentity,
};
