import { randomUUID } from "node:crypto";
import type { JsonObject, JsonValue } from "../../../agent-kernel/core/index.ts";

export const B13_HOST_PROTOCOL_VERSION = 1 as const;
export const B13_HOST_REQUEST_TYPE = "b13.host.request" as const;
export const B13_HOST_RESPONSE_TYPE = "b13.host.response" as const;

export type B13HostMethod =
  | "session.bind"
  | "session.startup"
  | "session.current_durable_event_seq"
  | "session.save_checkpoint"
  | "session.close"
  | "model.stream"
  | "model.count_tokens"
  | "tools.list"
  | "tool.describe"
  | "tool.validate"
  | "tool.invoke"
  | "events.publish"
  | "events.audit"
  | "telemetry.record";

export type B13HostRequest = {
  schemaVersion: 1;
  type: typeof B13_HOST_REQUEST_TYPE;
  requestId: string;
  taskId: string;
  operationKey: string;
  method: B13HostMethod;
  payload: JsonObject;
};

export type B13HostResponse = {
  schemaVersion: 1;
  type: typeof B13_HOST_RESPONSE_TYPE;
  requestId: string;
  taskId: string;
  operationKey: string;
  ok: boolean;
  payload: JsonObject;
  errorCode?: string;
  message?: string;
};

export interface B13HostPort {
  postMessage(value: unknown): void;
  on(event: "message" | "messageerror", listener: (value: unknown) => void): this;
  close(): void;
}

export type B13HostRequestHandler = (request: B13HostRequest) => Promise<JsonObject>;

const METHODS = new Set<B13HostMethod>([
  "session.bind",
  "session.startup",
  "session.current_durable_event_seq",
  "session.save_checkpoint",
  "session.close",
  "model.stream",
  "model.count_tokens",
  "tools.list",
  "tool.describe",
  "tool.validate",
  "tool.invoke",
  "events.publish",
  "events.audit",
  "telemetry.record",
]);

function isJsonValue(value: unknown, seen = new WeakSet<object>()): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, seen));
  return Object.values(value).every((item) => isJsonValue(item, seen));
}

function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) && isJsonValue(value);
}

function nonEmpty(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length < 1 || value.length > 256) throw new Error(`B13_HOST_PROTOCOL_INVALID: ${field}`);
}

function rejectSecretFields(value: unknown, seen = new WeakSet<object>()): void {
  if (value === null || typeof value !== "object") return;
  if (seen.has(value)) throw new Error("B13_HOST_PROTOCOL_INVALID: cyclic payload");
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) rejectSecretFields(item, seen);
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    if (["apiKey", "api_key", "rawCredential", "raw_credential", "authorization", "headers", "endpoint", "baseUrl", "base_url", "HOME", "PATH"].includes(key)) {
      throw new Error(`B13_HOST_PROTOCOL_INVALID: forbidden field ${key}`);
    }
    rejectSecretFields(child, seen);
  }
}

export function validateB13HostRequest(value: unknown): B13HostRequest {
  if (!isJsonObject(value)) throw new Error("B13_HOST_PROTOCOL_INVALID: request object");
  if (value.schemaVersion !== B13_HOST_PROTOCOL_VERSION || value.type !== B13_HOST_REQUEST_TYPE) throw new Error("B13_HOST_PROTOCOL_INVALID: version/type");
  nonEmpty(value.requestId, "requestId");
  nonEmpty(value.taskId, "taskId");
  nonEmpty(value.operationKey, "operationKey");
  if (typeof value.method !== "string" || !METHODS.has(value.method as B13HostMethod)) throw new Error("B13_HOST_PROTOCOL_INVALID: method");
  if (!isJsonObject(value.payload)) throw new Error("B13_HOST_PROTOCOL_INVALID: payload");
  rejectSecretFields(value.payload);
  return value as unknown as B13HostRequest;
}

export function validateB13HostResponse(value: unknown): B13HostResponse {
  if (!isJsonObject(value)) throw new Error("B13_HOST_PROTOCOL_INVALID: response object");
  if (value.schemaVersion !== B13_HOST_PROTOCOL_VERSION || value.type !== B13_HOST_RESPONSE_TYPE) throw new Error("B13_HOST_PROTOCOL_INVALID: response version/type");
  nonEmpty(value.requestId, "requestId");
  nonEmpty(value.taskId, "taskId");
  nonEmpty(value.operationKey, "operationKey");
  if (typeof value.ok !== "boolean" || !isJsonObject(value.payload)) throw new Error("B13_HOST_PROTOCOL_INVALID: response payload");
  rejectSecretFields(value.payload);
  return value as unknown as B13HostResponse;
}

export class B13HostClient {
  private readonly pending = new Map<string, { resolve: (value: JsonObject) => void; reject: (error: unknown) => void }>();
  private readonly port: B13HostPort;
  private readonly taskId: string;
  private readonly operationKey: string;

  constructor(port: B13HostPort, taskId: string, operationKey: string) {
    this.port = port;
    this.taskId = taskId;
    this.operationKey = operationKey;
    port.on("message", (value) => this.handle(value));
    port.on("messageerror", (error) => this.failAll(error));
  }

  async call(method: B13HostMethod, payload: JsonObject): Promise<JsonObject> {
    const request = validateB13HostRequest({
      schemaVersion: B13_HOST_PROTOCOL_VERSION,
      type: B13_HOST_REQUEST_TYPE,
      requestId: `${method}-${randomUUID()}`,
      taskId: this.taskId,
      operationKey: this.operationKey,
      method,
      payload,
    });
    return new Promise<JsonObject>((resolve, reject) => {
      this.pending.set(request.requestId, { resolve, reject });
      try {
        this.port.postMessage(request);
      } catch (error) {
        this.pending.delete(request.requestId);
        reject(error);
      }
    });
  }

  close(): void {
    this.failAll(new Error("B13_HOST_IPC_CLOSED"));
    this.port.close();
  }

  private handle(value: unknown): void {
    let response: B13HostResponse;
    try {
      response = validateB13HostResponse(value);
    } catch (error) {
      this.failAll(error);
      return;
    }
    if (response.taskId !== this.taskId || response.operationKey !== this.operationKey) {
      this.failAll(new Error("B13_HOST_IDENTITY_MISMATCH"));
      return;
    }
    const pending = this.pending.get(response.requestId);
    if (!pending) return;
    this.pending.delete(response.requestId);
    if (response.ok) pending.resolve(response.payload);
    else {
      const error = new Error(response.message ?? "B13 host request failed");
      Object.assign(error, { code: response.errorCode ?? "B13_HOST_REQUEST_FAILED" });
      pending.reject(error);
    }
  }

  private failAll(error: unknown): void {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
}
