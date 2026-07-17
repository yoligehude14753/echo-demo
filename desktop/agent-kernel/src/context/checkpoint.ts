import { sha256Json, stableJson } from "./hash.ts";
import {
  cloneCanonicalMessages,
  CONTEXT_SCHEMA_VERSION,
} from "./types.ts";
import type {
  CheckpointValidationContext,
  ContextBudgetState,
  ContextCheckpointBody,
  ContextCheckpointPayload,
  ContextCompactState,
} from "./types.ts";

const FORBIDDEN_CHECKPOINT_KEYS = new Set([
  "apiKey",
  "api_key",
  "credential",
  "credentialHandle",
  "globalConfig",
  "global_config",
  "HOME",
  "PATH",
  "pid",
  "processId",
  "rawCredential",
  "raw_credential",
  "sessionFile",
  "sessionPath",
  "temporaryPort",
  "tempPort",
]);

export type CreateContextCheckpointInput = ContextCheckpointBody;

export class ContextCheckpointError extends Error {
  readonly code:
    | "CHECKPOINT_CORRUPT"
    | "CHECKPOINT_TASK_MISMATCH"
    | "CHECKPOINT_OPERATION_MISMATCH"
    | "CHECKPOINT_MODEL_REVISION_MISSING"
    | "GRANT_REVISION_MISMATCH"
    | "CHECKPOINT_EVENT_SEQ_AHEAD"
    | "GRANT_EXPIRED";

  constructor(
    code: ContextCheckpointError["code"],
    message: string,
  ) {
    super(message);
    this.name = "ContextCheckpointError";
    this.code = code;
  }
}

function scanForbiddenKeys(value: unknown, seen = new WeakSet<object>()): string | undefined {
  if (value === null || typeof value !== "object") return undefined;
  if (seen.has(value)) return undefined;
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = scanForbiddenKeys(item, seen);
      if (found) return found;
    }
    return undefined;
  }
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_CHECKPOINT_KEYS.has(key)) return key;
    const found = scanForbiddenKeys(child, seen);
    if (found) return found;
  }
  return undefined;
}

function assertNonNegativeInteger(value: number, name: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", `${name} is invalid`);
  }
}

function validateState(
  compactState: ContextCompactState,
  budgetState: ContextBudgetState,
): void {
  if (
    compactState.schemaVersion !== CONTEXT_SCHEMA_VERSION ||
    !["none", "microcompact"].includes(compactState.strategy) ||
    (compactState.summaryHash !== null && !/^[a-f0-9]{64}$/.test(compactState.summaryHash)) ||
    !Number.isSafeInteger(compactState.messageCountAtBoundary) ||
    compactState.messageCountAtBoundary < 0 ||
    new Set(compactState.clearedToolUseIds).size !== compactState.clearedToolUseIds.length
  ) {
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "compact state is invalid");
  }
  for (const value of [
    budgetState.turnsUsed,
    budgetState.toolCallsUsed,
    budgetState.modelInputTokens,
    budgetState.modelOutputTokens,
  ]) {
    assertNonNegativeInteger(value, "budget counter");
  }
}

function validateBody(body: ContextCheckpointBody): void {
  if (
    body.schemaVersion !== CONTEXT_SCHEMA_VERSION ||
    !body.checkpointId ||
    !body.taskId ||
    !body.operationKey ||
    !body.createdAt ||
    !Number.isSafeInteger(body.modelConfigRevision) ||
    body.modelConfigRevision < 1 ||
    !Number.isSafeInteger(body.grantRevision) ||
    body.grantRevision < 1
  ) {
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "checkpoint identity is invalid");
  }
  if (!Number.isFinite(Date.parse(body.createdAt))) {
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "checkpoint timestamp is invalid");
  }
  assertNonNegativeInteger(body.lastDurableEventSeq, "lastDurableEventSeq");
  if (!Array.isArray(body.messages)) {
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "checkpoint messages are invalid");
  }
  validateState(body.compactState, body.budgetState);
  const forbidden = scanForbiddenKeys(body);
  if (forbidden) {
    throw new ContextCheckpointError(
      "CHECKPOINT_CORRUPT",
      `checkpoint contains forbidden field: ${forbidden}`,
    );
  }
}

function checkpointBody(payload: ContextCheckpointPayload): ContextCheckpointBody {
  const body = { ...payload } as Partial<ContextCheckpointPayload>;
  delete body.checksum;
  return body;
}

export async function checkpointChecksum(body: ContextCheckpointBody): Promise<string> {
  validateBody(body);
  return sha256Json(body);
}

export async function createCheckpointPayload(
  input: CreateContextCheckpointInput,
): Promise<ContextCheckpointPayload> {
  const body: ContextCheckpointBody = {
    ...input,
    messages: cloneCanonicalMessages(input.messages),
    compactState: {
      ...input.compactState,
      clearedToolUseIds: [...input.compactState.clearedToolUseIds],
    },
    budgetState: { ...input.budgetState },
  };
  const checksum = await checkpointChecksum(body);
  return { ...body, checksum };
}

export async function verifyCheckpointChecksum(
  payload: ContextCheckpointPayload,
): Promise<void> {
  try {
    validateBody(checkpointBody(payload));
    const expected = await checkpointChecksum(checkpointBody(payload));
    if (payload.checksum !== expected) {
      throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "checkpoint checksum mismatch");
    }
  } catch (error) {
    if (error instanceof ContextCheckpointError) throw error;
    throw new ContextCheckpointError("CHECKPOINT_CORRUPT", "checkpoint verification failed");
  }
}

export async function validateCheckpointPayload(
  payload: ContextCheckpointPayload,
  context: CheckpointValidationContext,
): Promise<void> {
  await verifyCheckpointChecksum(payload);
  if (payload.taskId !== context.taskId) {
    throw new ContextCheckpointError("CHECKPOINT_TASK_MISMATCH", "checkpoint task identity does not match");
  }
  if (payload.operationKey !== context.operationKey) {
    throw new ContextCheckpointError("CHECKPOINT_OPERATION_MISMATCH", "checkpoint operation identity does not match");
  }
  if (payload.modelConfigRevision !== context.modelConfigRevision) {
    throw new ContextCheckpointError("CHECKPOINT_MODEL_REVISION_MISSING", "checkpoint model revision does not match");
  }
  if (payload.grantRevision !== context.grantRevision) {
    throw new ContextCheckpointError("GRANT_REVISION_MISMATCH", "checkpoint grant revision does not match");
  }
  if (payload.lastDurableEventSeq > context.currentDurableEventSeq) {
    throw new ContextCheckpointError("CHECKPOINT_EVENT_SEQ_AHEAD", "checkpoint durable sequence is ahead of the session");
  }
  if (context.grantExpiresAt) {
    const now = Date.parse(context.now ?? new Date().toISOString());
    const expiresAt = Date.parse(context.grantExpiresAt);
    if (!Number.isFinite(now) || !Number.isFinite(expiresAt) || expiresAt <= now) {
      throw new ContextCheckpointError("GRANT_EXPIRED", "grant snapshot is expired");
    }
  }
}

export function checkpointBodyForEvidence(payload: ContextCheckpointPayload): string {
  return stableJson(checkpointBody(payload));
}
