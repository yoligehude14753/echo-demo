import type { JsonObject, JsonValue, KernelBuildIdentity } from "../../../agent-kernel/core/index.ts";

export const WORKER_PROTOCOL_VERSION = 1 as const;
export const MAX_RUNTIME_FRAME_BYTES = 8 * 1024 * 1024;

export type RuntimeFrameType =
  | "ready"
  | "open"
  | "opened"
  | "turn"
  | "event"
  | "turn_end"
  | "checkpoint"
  | "checkpointed"
  | "cancel"
  | "cancelled"
  | "close"
  | "closed"
  | "error";

export type RuntimeFrame = {
  schemaVersion: 1;
  type: RuntimeFrameType;
  requestId: string;
  taskId: string;
  operationKey: string;
  payload: JsonObject;
  buildIdentity?: KernelBuildIdentity;
  runtimeEventId?: string;
};

export class RuntimeProtocolError extends Error {
  readonly code: "RUNTIME_INVALID_FRAME" | "RUNTIME_FRAME_TOO_LARGE";

  constructor(code: RuntimeProtocolError["code"], message: string) {
    super(message);
    this.name = "RuntimeProtocolError";
    this.code = code;
  }
}

const FRAME_TYPES = new Set<RuntimeFrameType>([
  "ready",
  "open",
  "opened",
  "turn",
  "event",
  "turn_end",
  "checkpoint",
  "checkpointed",
  "cancel",
  "cancelled",
  "close",
  "closed",
  "error",
]);

const FRAME_FIELDS = new Set(["schemaVersion", "type", "requestId", "taskId", "operationKey", "payload", "buildIdentity", "runtimeEventId"]);

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isJsonValue(value: unknown, seen = new WeakSet<object>()): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, seen));
  return Object.values(value).every((item) => isJsonValue(item, seen));
}

function assertNonEmptyString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > 256) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", `${field} must be a non-empty bounded string`);
  }
}

export function runtimeFrameByteLength(frame: RuntimeFrame): number {
  return new TextEncoder().encode(JSON.stringify(frame)).byteLength;
}

export function validateRuntimeFrame(value: unknown): RuntimeFrame {
  if (!isObject(value)) throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "runtime frame must be an object");
  if (Object.keys(value).some((key) => !FRAME_FIELDS.has(key))) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "runtime frame contains an unknown field");
  }
  if (value.schemaVersion !== WORKER_PROTOCOL_VERSION) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "runtime frame schemaVersion is unsupported");
  }
  if (typeof value.type !== "string" || !FRAME_TYPES.has(value.type as RuntimeFrameType)) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "runtime frame type is unsupported");
  }
  assertNonEmptyString(value.requestId, "requestId");
  assertNonEmptyString(value.taskId, "taskId");
  assertNonEmptyString(value.operationKey, "operationKey");
  if (!isObject(value.payload) || !isJsonValue(value.payload)) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "runtime frame payload must be a JSON object");
  }
  if (value.runtimeEventId !== undefined) assertNonEmptyString(value.runtimeEventId, "runtimeEventId");
  if (value.buildIdentity !== undefined && !isObject(value.buildIdentity)) {
    throw new RuntimeProtocolError("RUNTIME_INVALID_FRAME", "buildIdentity must be an object");
  }
  const frame = value as RuntimeFrame;
  if (runtimeFrameByteLength(frame) > MAX_RUNTIME_FRAME_BYTES) {
    throw new RuntimeProtocolError("RUNTIME_FRAME_TOO_LARGE", "runtime frame exceeds the size limit");
  }
  return frame;
}

export function makeRuntimeFrame(input: Omit<RuntimeFrame, "schemaVersion">): RuntimeFrame {
  const frame = { schemaVersion: WORKER_PROTOCOL_VERSION, ...input } satisfies RuntimeFrame;
  return validateRuntimeFrame(frame);
}
