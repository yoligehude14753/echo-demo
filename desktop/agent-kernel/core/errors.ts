import type { JsonObject, JsonValue } from "./types.ts";

export type KernelErrorCode =
  | "RUNTIME_UNAVAILABLE"
  | "RUNTIME_PROTOCOL_MISMATCH"
  | "RUNTIME_BUILD_MISMATCH"
  | "RUNTIME_WORKER_CRASHED"
  | "RUNTIME_FRAME_TOO_LARGE"
  | "RUNTIME_INVALID_FRAME"
  | "MODEL_CONFIG_INVALID"
  | "MODEL_CONFIG_REVISION_MISSING"
  | "MODEL_CREDENTIAL_MISSING"
  | "MODEL_CREDENTIAL_REVOKED"
  | "MODEL_PROTOCOL_UNSUPPORTED"
  | "MODEL_CAPABILITY_PROBE_FAILED"
  | "MODEL_TOOL_ARGUMENTS_INVALID"
  | "MODEL_TOOL_CORRELATION_MISMATCH"
  | "MODEL_REQUEST_ID_MISMATCH"
  | "MODEL_EVENT_UNKNOWN"
  | "MODEL_CONTEXT_EXCEEDED"
  | "MODEL_TIMEOUT"
  | "MODEL_UPSTREAM_ERROR"
  | "MODEL_CANCELLED"
  | "GRANT_MISSING"
  | "GRANT_EXPIRED"
  | "GRANT_REVOKED"
  | "GRANT_REVISION_MISMATCH"
  | "TOOL_NOT_REGISTERED"
  | "TOOL_CAPABILITY_DENIED"
  | "TOOL_PATH_OUTSIDE_WORKSPACE"
  | "TOOL_PATH_IDENTITY_CHANGED"
  | "TOOL_COMMAND_DENIED"
  | "TOOL_NETWORK_DENIED"
  | "TOOL_TIMEOUT"
  | "TOOL_OUTPUT_LIMIT"
  | "TOOL_CANCELLED"
  | "TOOL_EXECUTION_FAILED"
  | "CHECKPOINT_CORRUPT"
  | "CHECKPOINT_TASK_MISMATCH"
  | "CHECKPOINT_OPERATION_MISMATCH"
  | "CHECKPOINT_MODEL_REVISION_MISSING"
  | "CHECKPOINT_EVENT_SEQ_AHEAD"
  | "KERNEL_SESSION_CLOSED"
  | "KERNEL_TURN_ALREADY_ACTIVE"
  | "KERNEL_INPUT_INVALID";

export class KernelError extends Error {
  readonly code: KernelErrorCode;
  readonly details: JsonObject;

  constructor(code: KernelErrorCode, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "KernelError";
    this.code = code;
    this.details = details;
  }

  toJSON(): JsonObject {
    return {
      code: this.code,
      message: this.message,
      details: this.details,
    };
  }
}

export function isKernelError(error: unknown): error is KernelError {
  return error instanceof KernelError;
}

export function asJsonValue(value: unknown): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => asJsonValue(item));
  }
  if (typeof value === "object") {
    const result: JsonObject = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      result[key] = asJsonValue(item);
    }
    return result;
  }
  return String(value);
}

export function normalizeKernelError(error: unknown, fallback: KernelErrorCode, message: string): KernelError {
  if (isKernelError(error)) return error;
  return new KernelError(fallback, message);
}
