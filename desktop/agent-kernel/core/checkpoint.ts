import { KernelError } from "./errors.ts";
import type { KernelCheckpoint } from "./types.ts";

type CheckpointBody = Omit<KernelCheckpoint, "checksum">;

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
    .join(",")}}`;
}

async function sha256(value: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new KernelError("CHECKPOINT_CORRUPT", "checkpoint checksum capability unavailable");
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function checkpointChecksum(body: CheckpointBody): Promise<string> {
  return `sha256:${await sha256(canonicalJson(body))}`;
}

export async function verifyCheckpointChecksum(checkpoint: KernelCheckpoint): Promise<void> {
  const expected = await checkpointChecksum({
    schemaVersion: checkpoint.schemaVersion,
    checkpointId: checkpoint.checkpointId,
    taskId: checkpoint.taskId,
    operationKey: checkpoint.operationKey,
    modelConfigRevision: checkpoint.modelConfigRevision,
    grantRevision: checkpoint.grantRevision,
    lastDurableEventSeq: checkpoint.lastDurableEventSeq,
    messages: checkpoint.messages,
    compactState: checkpoint.compactState,
    budgetState: checkpoint.budgetState,
    createdAt: checkpoint.createdAt,
  });
  if (checkpoint.checksum !== expected) {
    throw new KernelError("CHECKPOINT_CORRUPT", "checkpoint checksum mismatch");
  }
}
