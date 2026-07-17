export const MODEL_RUNTIME_SCHEMA_VERSION = 1 as const;

export type ModelRuntimePurpose =
  | "agent_main"
  | "agent_compact"
  | "agent_summary"
  | "agent_quality"
  | "chat"
  | "minutes"
  | "memory";

export type ModelRuntimeProtocol = "openai_chat" | "anthropic_messages";

export interface ModelRuntimeIdentity {
  schemaVersion: 1;
  purpose: ModelRuntimePurpose;
  revision: number;
  configHash: string;
  routeId: string;
  protocol: ModelRuntimeProtocol;
  model: string;
}
export interface ModelRuntimeFallback {
  schemaVersion: 1;
  type: "agent.model.fallback";
  taskId: string;
  operationKey: string;
  configRevision: number;
  fromRouteId: string;
  toRouteId: string;
  reason: string;
  occurredAt: string;
}

export class ModelRuntimeContractError extends Error {
  readonly code: string;

  constructor(code: string, message = "model runtime contract rejected") {
    super(message);
    this.name = "ModelRuntimeContractError";
    this.code = code;
  }
}

const IDENTITY_KEYS = [
  "schemaVersion",
  "purpose",
  "revision",
  "configHash",
  "routeId",
  "protocol",
  "model",
] as const;

const FALLBACK_KEYS = [
  "schemaVersion",
  "type",
  "taskId",
  "operationKey",
  "configRevision",
  "fromRouteId",
  "toRouteId",
  "reason",
  "occurredAt",
] as const;

const PURPOSES = new Set<ModelRuntimePurpose>([
  "agent_main",
  "agent_compact",
  "agent_summary",
  "agent_quality",
  "chat",
  "minutes",
  "memory",
]);
const PROTOCOLS = new Set<ModelRuntimeProtocol>(["openai_chat", "anthropic_messages"]);

function reject(code: string, message: string): never {
  throw new ModelRuntimeContractError(code, message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assertExactKeys(value: Record<string, unknown>, expected: readonly string[]): void {
  const expectedSet = new Set(expected);
  const actual = Object.keys(value);
  if (
    actual.some((key) => !expectedSet.has(key)) ||
    expected.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
  ) {
    reject("MODEL_RUNTIME_SCHEMA_VERSION_MISMATCH", "model runtime payload has unknown or missing fields");
  }
}

function nonEmpty(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", `${field} must be non-empty`);
  }
  return value;
}

function positiveInteger(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", `${field} must be a positive integer`);
  }
  return value as number;
}

export function parseModelRuntimeIdentity(value: unknown): ModelRuntimeIdentity {
  if (!isRecord(value)) reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime identity must be an object");
  assertExactKeys(value, IDENTITY_KEYS);
  if (value.schemaVersion !== MODEL_RUNTIME_SCHEMA_VERSION) {
    reject("MODEL_SCHEMA_VERSION_MISMATCH", "model runtime identity schema is unsupported");
  }
  if (!PURPOSES.has(value.purpose as ModelRuntimePurpose)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime identity purpose is invalid");
  }
  if (!PROTOCOLS.has(value.protocol as ModelRuntimeProtocol)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime identity protocol is invalid");
  }
  const revision = positiveInteger(value.revision, "revision");
  const routeId = nonEmpty(value.routeId, "routeId");
  const model = nonEmpty(value.model, "model");
  if (typeof value.configHash !== "string" || !/^[0-9a-f]{64}$/.test(value.configHash)) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "configHash must be a lowercase sha256");
  }
  return Object.freeze({
    schemaVersion: 1,
    purpose: value.purpose as ModelRuntimePurpose,
    revision,
    configHash: value.configHash,
    routeId,
    protocol: value.protocol as ModelRuntimeProtocol,
    model,
  });
}

export function parseModelRuntimeFallback(value: unknown): ModelRuntimeFallback {
  if (!isRecord(value)) reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime fallback must be an object");
  assertExactKeys(value, FALLBACK_KEYS);
  if (value.schemaVersion !== MODEL_RUNTIME_SCHEMA_VERSION) {
    reject("MODEL_SCHEMA_VERSION_MISMATCH", "model runtime fallback schema is unsupported");
  }
  if (value.type !== "agent.model.fallback") {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "model runtime fallback type is invalid");
  }
  const taskId = nonEmpty(value.taskId, "taskId");
  const operationKey = nonEmpty(value.operationKey, "operationKey");
  const configRevision = positiveInteger(value.configRevision, "configRevision");
  const fromRouteId = nonEmpty(value.fromRouteId, "fromRouteId");
  const toRouteId = nonEmpty(value.toRouteId, "toRouteId");
  if (fromRouteId === toRouteId) {
    reject("MODEL_RUNTIME_CONTRACT_INVALID", "fallback routes must differ");
  }
  const reason = nonEmpty(value.reason, "reason");
  const occurredAt = nonEmpty(value.occurredAt, "occurredAt");
  return Object.freeze({
    schemaVersion: 1,
    type: "agent.model.fallback",
    taskId,
    operationKey,
    configRevision,
    fromRouteId,
    toRouteId,
    reason,
    occurredAt,
  });
}

export function modelRuntimeIdentityEqual(
  left: ModelRuntimeIdentity,
  right: ModelRuntimeIdentity,
): boolean {
  return (
    left.schemaVersion === right.schemaVersion &&
    left.purpose === right.purpose &&
    left.revision === right.revision &&
    left.configHash === right.configHash &&
    left.routeId === right.routeId &&
    left.protocol === right.protocol &&
    left.model === right.model
  );
}
