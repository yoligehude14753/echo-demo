import {
  decodeRuntimeFrame,
  encodeRuntimeFrame,
  makeRuntimeMessage,
  nonceProof,
  RuntimeFrameDecoder,
  type FramedRuntimeMessage,
  type RuntimeDuplex,
} from "./framed-runtime.ts";

export type EmbeddedRuntimeCommandHandler = {
  submit(input: {
    taskId: string;
    operationKey: string;
    payload: Record<string, unknown>;
    emit: (payload: Record<string, unknown>) => void;
  }): Promise<Record<string, unknown>>;
  cancel(input: { taskId: string; operationKey: string; payload: Record<string, unknown> }): Promise<Record<string, unknown>>;
  snapshot(input: { taskId: string; operationKey: string }): Promise<Record<string, unknown>>;
};

export class EmbeddedRuntimePortServer {
  private readonly decoder = new RuntimeFrameDecoder();
  private ready = false;
  private closed = false;

  constructor(
    private readonly duplex: RuntimeDuplex,
    private readonly nonce: string,
    private readonly handler: EmbeddedRuntimeCommandHandler,
  ) {}

  start(): void {
    this.duplex.on("data", (chunk) => {
      try {
        for (const frame of this.decoder.push(chunk)) void this.handle(frame);
      } catch (error) {
        this.degrade(error instanceof Error ? error.message : "invalid runtime frame");
      }
    });
    this.duplex.on("error", (error) => this.degrade(error.message));
  }

  emitEvent(taskId: string, operationKey: string, event: Record<string, unknown>): void {
    if (!this.ready || this.closed) return;
    this.send(makeRuntimeMessage("task.event", { event }, { taskId, operationKey }));
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.duplex.destroy();
  }

  private async handle(frame: FramedRuntimeMessage): Promise<void> {
    if (frame.type === "runtime.hello") {
      const proof = frame.payload.nonceProof;
      if (proof !== nonceProof(this.nonce)) {
        this.degrade("runtime nonce proof mismatch");
        return;
      }
      this.ready = true;
      this.send(makeRuntimeMessage("runtime.ready", { protocolVersion: 1, buildId: "echodesk-electron" }));
      return;
    }
    if (!this.ready) {
      this.degrade("runtime command arrived before handshake");
      return;
    }
    const taskId = frame.taskId;
    const operationKey = frame.operationKey;
    if (!taskId || !operationKey) {
      this.degrade("task frame misses task identity");
      return;
    }
    try {
      let type: string;
      let payload: Record<string, unknown>;
      if (frame.type === "task.submit") {
        type = "task.accepted";
        payload = await this.handler.submit({ taskId, operationKey, payload: frame.payload, emit: (event) => this.emitEvent(taskId, operationKey, event) });
      } else if (frame.type === "task.cancel") {
        type = "task.cancelled";
        payload = await this.handler.cancel({ taskId, operationKey, payload: frame.payload });
      } else if (frame.type === "task.snapshot.request") {
        type = "task.snapshot";
        payload = await this.handler.snapshot({ taskId, operationKey });
      } else {
        throw new Error(`unsupported runtime frame ${frame.type}`);
      }
      this.send(makeRuntimeMessage(type, payload, { taskId, operationKey }));
    } catch (error) {
      this.send(makeRuntimeMessage("runtime.degraded", { code: "RUNTIME_COMMAND_FAILED", message: error instanceof Error ? error.message : "runtime command failed" }, { taskId, operationKey }));
    }
  }

  private send(frame: FramedRuntimeMessage): void {
    if (this.closed) return;
    this.duplex.write(encodeRuntimeFrame(decodeRuntimeFrame(frame)));
  }

  private degrade(message: string): void {
    if (this.closed) return;
    try {
      this.send(makeRuntimeMessage("runtime.degraded", { code: "RUNTIME_PROTOCOL_ERROR", message }));
    } finally {
      this.close();
    }
  }
}
