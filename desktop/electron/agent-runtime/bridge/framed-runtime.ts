import { createHash, randomUUID } from "node:crypto";

export const RUNTIME_PROTOCOL_VERSION = 1 as const;
export const MAX_RUNTIME_FRAME_BYTES = 16 * 1024 * 1024;

export type FramedRuntimeMessage = {
  protocolVersion: 1;
  frameId: string;
  type: string;
  taskId?: string;
  operationKey?: string;
  sentAt: string;
  payload: Record<string, unknown>;
};

export type RuntimeDuplex = {
  on(event: "data", listener: (chunk: Buffer | Uint8Array | string) => void): RuntimeDuplex;
  on(event: "error", listener: (error: Error) => void): RuntimeDuplex;
  write(chunk: Buffer): boolean;
  destroy(error?: Error): void;
};

export class FramedRuntimeError extends Error {
  readonly code: "RUNTIME_FRAME_INVALID" | "RUNTIME_FRAME_TOO_LARGE";

  constructor(code: FramedRuntimeError["code"], message: string) {
    super(message);
    this.name = "FramedRuntimeError";
    this.code = code;
  }
}

function now(): string {
  return new Date().toISOString();
}

export function encodeRuntimeFrame(frame: FramedRuntimeMessage): Buffer {
  const body = Buffer.from(JSON.stringify(frame), "utf8");
  if (body.byteLength > MAX_RUNTIME_FRAME_BYTES) {
    throw new FramedRuntimeError("RUNTIME_FRAME_TOO_LARGE", "runtime frame exceeds 16 MiB");
  }
  const prefix = Buffer.allocUnsafe(4);
  prefix.writeUInt32BE(body.byteLength, 0);
  return Buffer.concat([prefix, body]);
}

export function decodeRuntimeFrame(value: unknown): FramedRuntimeMessage {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new FramedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame must be an object");
  }
  const frame = value as Record<string, unknown>;
  if (frame.protocolVersion !== RUNTIME_PROTOCOL_VERSION || typeof frame.frameId !== "string" || typeof frame.type !== "string" || typeof frame.sentAt !== "string") {
    throw new FramedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame identity is invalid");
  }
  if (frame.payload === null || typeof frame.payload !== "object" || Array.isArray(frame.payload)) {
    throw new FramedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame payload must be an object");
  }
  return frame as FramedRuntimeMessage;
}

export class RuntimeFrameDecoder {
  private pending = Buffer.alloc(0);

  push(chunk: Buffer | Uint8Array | string): FramedRuntimeMessage[] {
    this.pending = Buffer.concat([this.pending, Buffer.from(chunk)]);
    const frames: FramedRuntimeMessage[] = [];
    while (this.pending.byteLength >= 4) {
      const size = this.pending.readUInt32BE(0);
      if (size <= 0 || size > MAX_RUNTIME_FRAME_BYTES) {
        throw new FramedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame length is outside the contract");
      }
      if (this.pending.byteLength < size + 4) return frames;
      const body = this.pending.subarray(4, size + 4);
      this.pending = this.pending.subarray(size + 4);
      let parsed: unknown;
      try {
        parsed = JSON.parse(body.toString("utf8"));
      } catch {
        throw new FramedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame is not valid JSON");
      }
      frames.push(decodeRuntimeFrame(parsed));
    }
    return frames;
  }
}

export function makeRuntimeMessage(
  type: string,
  payload: Record<string, unknown>,
  identity: Pick<FramedRuntimeMessage, "taskId" | "operationKey"> = {},
): FramedRuntimeMessage {
  return {
    protocolVersion: RUNTIME_PROTOCOL_VERSION,
    frameId: randomUUID(),
    type,
    sentAt: now(),
    payload,
    ...identity,
  };
}

export function nonceProof(nonce: string): string {
  return createHash("sha256").update(nonce, "utf8").digest("hex");
}
