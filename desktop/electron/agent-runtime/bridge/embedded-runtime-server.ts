import {
  decodeRuntimeFrame,
  encodeRuntimeFrame,
  makeRuntimeMessage,
  nonceProof,
  RuntimeFrameDecoder,
  type FramedRuntimeMessage,
  type RuntimeDuplex,
} from "./framed-runtime.ts";
import {
  validateB13HostResponse,
  type B13HostRequest,
  type B13HostResponse,
} from "./b13-host-ipc.ts";
import type { JsonObject } from "../../../agent-kernel/core/index.ts";

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
  private readonly pendingHost = new Map<string, { resolve: (payload: JsonObject) => void; reject: (error: unknown) => void }>();
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

  requestHost(input: B13HostRequest): Promise<JsonObject> {
    if (!this.ready || this.closed) return Promise.reject(new Error("B13_HOST_IPC_UNAVAILABLE"));
    return new Promise<JsonObject>((resolve, reject) => {
      this.pendingHost.set(input.requestId, { resolve, reject });
      try {
        this.send(makeRuntimeMessage("runtime.host.request", { request: input }, { taskId: input.taskId, operationKey: input.operationKey }));
      } catch (error) {
        this.pendingHost.delete(input.requestId);
        reject(error);
      }
    });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const pending of this.pendingHost.values()) pending.reject(new Error("B13_HOST_IPC_CLOSED"));
    this.pendingHost.clear();
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
    if (frame.type === "runtime.host.response") {
      const raw = frame.payload.response;
      let response: B13HostResponse;
      try {
        response = validateB13HostResponse(raw);
      } catch (error) {
        this.degrade(error instanceof Error ? error.message : "invalid host response");
        return;
      }
      const pending = this.pendingHost.get(response.requestId);
      if (!pending) return;
      this.pendingHost.delete(response.requestId);
      if (response.taskId !== frame.taskId || response.operationKey !== frame.operationKey) {
        pending.reject(new Error("B13_HOST_IDENTITY_MISMATCH"));
      } else if (response.ok) {
        pending.resolve(response.payload);
      } else {
        const error = new Error(response.message ?? "B13 host request failed");
        Object.assign(error, { code: response.errorCode ?? "B13_HOST_REQUEST_FAILED" });
        pending.reject(error);
      }
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
