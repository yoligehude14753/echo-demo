import type {
  AgentTurnInput,
  CancelReason,
  KernelCheckpoint,
  KernelEventEnvelope,
  OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";
import { WorkerManager, WorkerRuntimeError, WorkerRuntimeSession, type WorkerManagerOptions } from "./worker-manager.ts";

export type WorkerPoolOptions = WorkerManagerOptions & { size: number };

type Slot = {
  manager: WorkerManager;
  busy: boolean;
};

type PendingLease = {
  input: OpenSessionInput;
  resolve: (session: PooledWorkerRuntimeSession) => void;
  reject: (error: unknown) => void;
};

export class PooledWorkerRuntimeSession {
  private closed = false;
  private inner: WorkerRuntimeSession;
  private readonly release: () => void;
  private readonly manager: WorkerManager;

  constructor(inner: WorkerRuntimeSession, release: () => void, manager: WorkerManager) {
    this.inner = inner;
    this.release = release;
    this.manager = manager;
  }

  get input(): OpenSessionInput {
    return this.inner.input;
  }

  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    if (this.closed) throw new WorkerRuntimeError("KERNEL_SESSION_CLOSED", "pooled worker session is closed");
    return this.inner.runTurn(input);
  }

  checkpoint(): Promise<KernelCheckpoint> {
    if (this.closed) return Promise.reject(new WorkerRuntimeError("KERNEL_SESSION_CLOSED", "pooled worker session is closed"));
    return this.inner.checkpoint();
  }

  cancel(reason: CancelReason): Promise<void> {
    if (this.closed) return Promise.resolve();
    return this.inner.cancel(reason);
  }

  async restart(): Promise<PooledWorkerRuntimeSession> {
    if (this.closed) throw new WorkerRuntimeError("KERNEL_SESSION_CLOSED", "pooled worker session is closed");
    const next = await this.inner.restart();
    return new PooledWorkerRuntimeSession(next, this.release, this.manager);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.inner.close();
    this.release();
  }
}

export class WorkerPool {
  private readonly slots: Slot[];
  private readonly waiters: PendingLease[] = [];
  private closed = false;

  constructor(options: WorkerPoolOptions) {
    if (!Number.isInteger(options.size) || options.size < 1) throw new Error("worker pool size must be a positive integer");
    this.slots = Array.from({ length: options.size }, () => ({ manager: new WorkerManager(options), busy: false }));
  }

  get busyCount(): number {
    return this.slots.filter((slot) => slot.busy).length;
  }

  async open(input: OpenSessionInput): Promise<PooledWorkerRuntimeSession> {
    if (this.closed) throw new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker pool is closed");
    const slot = this.slots.find((candidate) => !candidate.busy);
    if (!slot) {
      return new Promise<PooledWorkerRuntimeSession>((resolve, reject) => this.waiters.push({ input, resolve, reject }));
    }
    return this.openOnSlot(slot, input);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const failure = new WorkerRuntimeError("RUNTIME_UNAVAILABLE", "worker pool is closed");
    while (this.waiters.length) this.waiters.shift()!.reject(failure);
    await Promise.all(this.slots.map((slot) => slot.manager.close()));
  }

  private async openOnSlot(slot: Slot, input: OpenSessionInput): Promise<PooledWorkerRuntimeSession> {
    slot.busy = true;
    try {
      const session = await slot.manager.open(input);
      return new PooledWorkerRuntimeSession(session, () => this.release(slot), slot.manager);
    } catch (error) {
      slot.busy = false;
      this.pump();
      throw error;
    }
  }

  private release(slot: Slot): void {
    if (!slot.busy) return;
    slot.busy = false;
    this.pump();
  }

  private pump(): void {
    if (this.closed || this.waiters.length === 0) return;
    const slot = this.slots.find((candidate) => !candidate.busy);
    if (!slot) return;
    const waiter = this.waiters.shift()!;
    void this.openOnSlot(slot, waiter.input).then(waiter.resolve, waiter.reject);
  }
}
