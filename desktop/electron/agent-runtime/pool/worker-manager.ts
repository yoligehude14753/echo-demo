import { randomUUID } from "node:crypto";
import { MessageChannel, Worker } from "node:worker_threads";
import { MessagePortChannel, type RuntimeMessagePort } from "../message-port/channel.ts";
import type { RuntimeFrame, RuntimeFrameType } from "../message-port/envelope.ts";
import type {
  AgentTurnInput,
  CancelReason,
  JsonObject,
  KernelCheckpoint,
  KernelEventEnvelope,
  KernelSession,
  OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";
import {
  assertRuntimeManifestMatches,
  type RuntimeManifest,
  validateRuntimeManifest,
} from "../worker/identity.ts";
import {
  B13_HOST_PROTOCOL_VERSION,
  B13_HOST_RESPONSE_TYPE,
  validateB13HostRequest,
  type B13HostRequest,
  type B13HostRequestHandler,
  type B13HostPort,
} from "../bridge/b13-host-ipc.ts";

export type WorkerManagerState = "new" | "starting" | "ready" | "open" | "crashed" | "closed";

export type WorkerManagerOptions = {
  manifest: RuntimeManifest;
  factoryModule: URL | string;
  factoryExport?: string;
  /** Task-owned, secret-free data for the worker-local host factory. */
  factoryData?: JsonObject;
  /** Parent-side bridge to the inherited-FD Python host; absent means fail closed. */
  hostRequest?: B13HostRequestHandler;
  workerEntry?: URL | string;
  startupTimeoutMs?: number;
};

export class WorkerRuntimeError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "WorkerRuntimeError";
    this.code = code;
  }
}

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

class AsyncQueue<T> implements AsyncIterable<T> {
  private readonly values: T[] = [];
  private readonly waiters: Array<{
    resolve: (result: IteratorResult<T>) => void;
    reject: (error: unknown) => void;
  }> = [];
  private failure: unknown;
  private ended = false;

  push(value: T): void {
    if (this.ended) return;
    const waiter = this.waiters.shift();
    if (waiter) waiter.resolve({ done: false, value });
    else this.values.push(value);
  }

  end(): void {
    if (this.ended) return;
    this.ended = true;
    while (this.waiters.length) this.waiters.shift()!.resolve({ done: true, value: undefined });
  }

  fail(error: unknown): void {
    if (this.ended) return;
    this.failure = error;
    this.ended = true;
    while (this.waiters.length) this.waiters.shift()!.reject(error);
  }

  next(): Promise<IteratorResult<T>> {
    if (this.values.length) return Promise.resolve({ done: false, value: this.values.shift()! });
    if (this.ended) {
      if (this.failure) return Promise.reject(this.failure);
      return Promise.resolve({ done: true, value: undefined });
    }
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return { next: () => this.next() };
  }
}

type ActiveTurn = {
  requestId: string;
  queue: AsyncQueue<KernelEventEnvelope>;
};

type PendingRequest = Deferred<RuntimeFrame> & { type: RuntimeFrameType };

function workerPort(worker: Worker): RuntimeMessagePort {
  return {
    postMessage(value: unknown): void {
      worker.postMessage(value);
    },
    on(event: "message" | "messageerror", listener: (value: unknown) => void): RuntimeMessagePort {
      worker.on(event, listener);
      return this;
    },
    close(): void {
      void worker.terminate();
    },
  };
}

function isKernelEvent(value: unknown): value is KernelEventEnvelope {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const event = value as Record<string, unknown>;
  return event.schemaVersion === 1 && typeof event.taskId === "string" && typeof event.operationKey === "string" && typeof event.runtimeEventId === "string" && typeof event.type === "string";
}

export class WorkerRuntimeSession implements KernelSession {
  private closed = false;
  private readonly manager: WorkerManager;
  readonly input: OpenSessionInput;

  constructor(manager: WorkerManager, input: OpenSessionInput) {
    this.manager = manager;
    this.input = input;
  }

  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    if (this.closed) throw new WorkerRuntimeError("KERNEL_SESSION_CLOSED", "worker session is closed");
    return this.manager.runTurnStream(input);
  }

  checkpoint(): Promise<KernelCheckpoint> {
    if (this.closed) return Promise.reject(new WorkerRuntimeError("KERNEL_SESSION_CLOSED", "worker session is closed"));
    return this.manager.checkpoint();
  }

  cancel(reason: CancelReason): Promise<void> {
    if (this.closed) return Promise.resolve();
    return this.manager.cancel(reason);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.manager.closeSession();
  }

  async restart(): Promise<WorkerRuntimeSession> {
    if (!this.closed) await this.close();
    return this.manager.restart(this.input);
  }
}

export class WorkerManager {
  private readonly options: WorkerManagerOptions;
  private worker: Worker | undefined;
  private channel: MessagePortChannel | undefined;
  private state: WorkerManagerState = "new";
  private ready: Deferred<RuntimeFrame> | undefined;
  private activeTurn: ActiveTurn | undefined;
  private readonly pending = new Map<string, PendingRequest>();
  private hostPort: B13HostPort | undefined;
  private openInput: OpenSessionInput | undefined;
  private closing = false;

  constructor(options: WorkerManagerOptions) {
    this.options = options;
    validateRuntimeManifest(options.manifest);
  }

  get currentState(): WorkerManagerState {
    return this.state;
  }

  async start(): Promise<void> {
    if (this.state === "ready" || this.state === "open") return;
    if (this.state === "closed") throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker manager is closed");
    this.state = "starting";
    const hostChannel = new MessageChannel();
    this.hostPort = hostChannel.port1;
    hostChannel.port1.on("message", (value) => void this.handleHostRequest(value, hostChannel.port1));
    hostChannel.port1.on("messageerror", (error) => this.handleCrash(error));
    const worker = new Worker(this.options.workerEntry ?? new URL("../worker/worker-entry.ts", import.meta.url), {
      execArgv: process.execArgv.filter((arg) => arg !== "--input-type=module" && arg !== "--input-type=commonjs"),
      workerData: {
        manifest: this.options.manifest,
        factoryModule: String(this.options.factoryModule),
        factoryExport: this.options.factoryExport ?? "createWorkerRuntime",
        factoryData: this.options.factoryData,
        hostPort: hostChannel.port2,
      },
      transferList: [hostChannel.port2],
    });
    this.worker = worker;
    this.channel = new MessagePortChannel(
      workerPort(worker),
      (frame) => this.handleFrame(frame),
      (error) => this.handleCrash(error),
    );
    worker.on("error", (error) => this.handleCrash(error));
    worker.on("exit", (code) => {
      if (!this.closing && code !== 0) this.handleCrash(new WorkerRuntimeError("RUNTIME_WORKER_CRASHED", `worker exited with code ${code}`));
    });
    this.ready = deferred<RuntimeFrame>();
    const timeoutMs = this.options.startupTimeoutMs ?? 10_000;
    const timeout = setTimeout(() => this.ready?.reject(new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker startup timed out")), timeoutMs);
    try {
      const frame = await this.ready.promise;
      if (!frame.buildIdentity || frame.payload.manifestId !== this.options.manifest.manifestId) throw new WorkerRuntimeError("RUNTIME_BUILD_MISMATCH", "worker manifest identity mismatch");
      assertRuntimeManifestMatches(this.options.manifest, { ...this.options.manifest, buildIdentity: frame.buildIdentity });
      this.state = "ready";
    } catch (error) {
      await this.stopWorker();
      this.state = "crashed";
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async open(input: OpenSessionInput): Promise<WorkerRuntimeSession> {
    await this.start();
    if (this.state !== "ready") throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", `worker manager is not ready: ${this.state}`);
    this.openInput = input;
    let frame: RuntimeFrame;
    try {
      frame = await this.request("open", input.taskId, input.operationKey, { open: input }, this.options.manifest.buildIdentity);
    } catch (error) {
      this.openInput = undefined;
      throw error;
    }
    if (frame.type !== "opened" || !frame.buildIdentity) throw new WorkerRuntimeError("RUNTIME_INVALID_FRAME", "worker open acknowledgement is invalid");
    assertRuntimeManifestMatches(this.options.manifest, { ...this.options.manifest, buildIdentity: frame.buildIdentity });
    this.state = "open";
    return new WorkerRuntimeSession(this, input);
  }

  async restart(input = this.openInput): Promise<WorkerRuntimeSession> {
    if (!input) throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker has no open session to restart");
    await this.stopWorker();
    this.state = "new";
    return this.open(input);
  }

  runTurnStream(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    if (this.state !== "open") throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker session is not open");
    if (this.activeTurn) throw new WorkerRuntimeError("KERNEL_TURN_ALREADY_ACTIVE", "worker turn is already active");
    if (input.taskId !== this.openInput?.taskId || input.operationKey !== this.openInput?.operationKey) {
      throw new WorkerRuntimeError("KERNEL_INPUT_INVALID", "turn identity does not match worker session");
    }
    const queue = new AsyncQueue<KernelEventEnvelope>();
    const requestId = `turn-${randomUUID()}`;
    this.activeTurn = { requestId, queue };
    try {
      this.channel!.send({ type: "turn", requestId, taskId: input.taskId, operationKey: input.operationKey, payload: { turn: { input } } as unknown as JsonObject });
    } catch (error) {
      this.activeTurn = undefined;
      queue.fail(error);
    }
    return queue;
  }

  async checkpoint(): Promise<KernelCheckpoint> {
    const frame = await this.request("checkpoint", this.requireOpenTask(), this.requireOpenOperation(), { request: "checkpoint" });
    const checkpoint = frame.payload.checkpoint;
    if (checkpoint === null || typeof checkpoint !== "object" || Array.isArray(checkpoint)) throw new WorkerRuntimeError("RUNTIME_INVALID_FRAME", "checkpoint response is invalid");
    return checkpoint as KernelCheckpoint;
  }

  async cancel(reason: CancelReason): Promise<void> {
    if (!this.activeTurn) return;
    await this.request("cancel", this.requireOpenTask(), this.requireOpenOperation(), { reason });
  }

  async closeSession(): Promise<void> {
    if (this.state !== "open") return;
    if (this.activeTurn) await this.cancel("user");
    await this.request("close", this.requireOpenTask(), this.requireOpenOperation(), { close: true });
    this.state = "ready";
    this.openInput = undefined;
  }

  async close(): Promise<void> {
    this.state = "closed";
    await this.stopWorker();
  }

  private requireOpenTask(): string {
    if (!this.openInput) throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker session is not open");
    return this.openInput.taskId;
  }

  private requireOpenOperation(): string {
    if (!this.openInput) throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker session is not open");
    return this.openInput.operationKey;
  }

  private request(type: RuntimeFrameType, taskId: string, operationKey: string, payload: JsonObject, buildIdentity?: RuntimeManifest["buildIdentity"]): Promise<RuntimeFrame> {
    const requestId = `${type}-${randomUUID()}`;
    const pending = deferred<RuntimeFrame>() as PendingRequest;
    pending.type = type;
    this.pending.set(requestId, pending);
    try {
      this.channel!.send({ type, requestId, taskId, operationKey, payload, buildIdentity });
    } catch (error) {
      this.pending.delete(requestId);
      pending.reject(error);
    }
    return pending.promise.finally(() => this.pending.delete(requestId));
  }

  private handleFrame(frame: RuntimeFrame): void {
    if (frame.type === "ready") {
      this.ready?.resolve(frame);
      return;
    }
    if (frame.type === "event") {
      if (!this.activeTurn || frame.requestId !== this.activeTurn.requestId) return;
      if (!isKernelEvent(frame.payload)) {
        this.activeTurn.queue.fail(new WorkerRuntimeError("RUNTIME_INVALID_FRAME", "worker event payload is invalid"));
        this.activeTurn = undefined;
        return;
      }
      this.activeTurn.queue.push(frame.payload);
      return;
    }
    if (frame.type === "turn_end") {
      if (this.activeTurn?.requestId === frame.requestId) {
        this.activeTurn.queue.end();
        this.activeTurn = undefined;
      }
      return;
    }
    if (frame.type === "error") {
      const code = typeof frame.payload.code === "string" ? frame.payload.code : "RUNTIME_WORKER_REQUEST_FAILED";
      const message = typeof frame.payload.message === "string" ? frame.payload.message : "worker request failed";
      const failure = new WorkerRuntimeError(code, message);
      if (this.activeTurn?.requestId === frame.requestId) {
        this.activeTurn.queue.fail(failure);
        this.activeTurn = undefined;
      }
      const pending = this.pending.get(frame.requestId);
      if (pending) pending.reject(failure);
      else if (this.state === "starting") this.ready?.reject(failure);
      return;
    }
    const pending = this.pending.get(frame.requestId);
    if (pending) pending.resolve(frame);
  }

  private async handleHostRequest(value: unknown, port: B13HostPort): Promise<void> {
    let request: B13HostRequest;
    try {
      request = validateB13HostRequest(value);
    } catch (error) {
      this.handleCrash(error);
      return;
    }
    const response: Record<string, unknown> = {
      schemaVersion: B13_HOST_PROTOCOL_VERSION,
      type: B13_HOST_RESPONSE_TYPE,
      requestId: request.requestId,
      taskId: request.taskId,
      operationKey: request.operationKey,
      ok: false,
      payload: {},
    };
    if (request.taskId !== this.openInput?.taskId || request.operationKey !== this.openInput?.operationKey) {
      response.errorCode = "B13_HOST_IDENTITY_MISMATCH";
      response.message = "host request identity does not match the open worker session";
    } else if (!this.options.hostRequest) {
      response.errorCode = "B13_HOST_IPC_UNBOUND";
      response.message = "parent host request bridge is not bound";
    } else {
      try {
        response.payload = await this.options.hostRequest(request);
        response.ok = true;
      } catch (error) {
        response.errorCode = typeof (error as { code?: unknown })?.code === "string" ? String((error as { code: string }).code) : "B13_HOST_REQUEST_FAILED";
        response.message = error instanceof Error ? error.message : "host request failed";
      }
    }
    try {
      port.postMessage(response);
    } catch (error) {
      this.handleCrash(error);
    }
  }

  private handleCrash(error: unknown): void {
    if (this.closing || this.state === "closed") return;
    const failure = error instanceof WorkerRuntimeError ? error : new WorkerRuntimeError("RUNTIME_WORKER_CRASHED", error instanceof Error ? error.message : "worker crashed");
    this.state = "crashed";
    this.activeTurn?.queue.fail(failure);
    this.activeTurn = undefined;
    this.ready?.reject(failure);
    for (const request of this.pending.values()) request.reject(failure);
    this.pending.clear();
  }

  private async stopWorker(): Promise<void> {
    this.closing = true;
    const worker = this.worker;
    this.channel?.close();
    this.hostPort?.close();
    this.worker = undefined;
    this.channel = undefined;
    this.hostPort = undefined;
    this.ready = undefined;
    this.activeTurn?.queue.fail(new WorkerRuntimeError("RUNTIME_WORKER_CRASHED", "worker stopped"));
    this.activeTurn = undefined;
    for (const request of this.pending.values()) request.reject(new WorkerRuntimeError("RUNTIME_WORKER_CRASHED", "worker stopped"));
    this.pending.clear();
    if (worker) await worker.terminate();
    this.closing = false;
  }
}
