import {
  CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
  CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS,
  CAPTURE_UPLOAD_RECOVERY_PARTITION_CONCURRENCY,
  captureRetryDelay,
  type CaptureSpoolInventory,
  type CaptureSpoolItem,
  type CaptureSpoolPartitionSummary,
  type CaptureSpoolSnapshot,
  type CaptureUploadSpool,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./captureUploadSpool.ts";

export interface CaptureUploadCoordinatorHandlers<TResult = void> {
  upload(item: CaptureSpoolItem, signal: AbortSignal): Promise<TResult>;
  /** A fulfilled HTTP response must still prove durable remote admission. */
  isAcknowledgementValid?(item: CaptureSpoolItem, result: TResult): boolean;
  beforeAttempt?(item: CaptureSpoolItem): number | null;
  retryDelay?(error: unknown, retryCount: number): number | null;
  shouldRetry?(error: unknown): boolean;
  onAttempt?(item: CaptureSpoolItem): void;
  onAcknowledged?(item: CaptureSpoolItem, result: TResult): void;
  onRetry?(
    item: CaptureSpoolItem,
    retryCount: number,
    error: unknown,
    nextAttemptAtMs: number,
  ): void;
  onDiscarded?(item: CaptureSpoolItem, error: unknown): void;
  onSettled?(item: CaptureSpoolItem, latencyMs: number): void;
  onIdle?(): void;
  onSnapshot?(snapshot: CaptureSpoolSnapshot, partition?: string): void;
  onExpired?(count: number, partition?: string): void;
  onError?(error: unknown, partition?: string): void;
}

export class CaptureUploadReceiptError extends Error {
  constructor() {
    super("durable capture receipt unavailable");
    this.name = "CaptureUploadReceiptError";
  }
}

export interface CaptureUploadCoordinatorOptions {
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  cancelSchedule?: (handle: ReturnType<typeof setTimeout>) => void;
  maxConcurrentUploads?: number;
}

export class CaptureUploadCoordinator<TResult = void> {
  private readonly spool: CaptureUploadSpool;
  private readonly handlers: CaptureUploadCoordinatorHandlers<TResult>;
  private readonly now: () => number;
  private readonly schedule: NonNullable<CaptureUploadCoordinatorOptions["schedule"]>;
  private readonly cancelSchedule: NonNullable<CaptureUploadCoordinatorOptions["cancelSchedule"]>;
  private readonly maxConcurrentUploads: number;
  private partition: string | null = null;
  private running = false;
  private draining = false;
  private kickRequested = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly activeAborts = new Set<AbortController>();
  private inFlightCount = 0;
  private readonly idleWaiters = new Set<() => void>();
  private lockAbort: AbortController | null = null;
  private generation = 0;
  private readonly remoteAcknowledgements = new Map<number, TResult>();

  constructor(
    spool: CaptureUploadSpool,
    handlers: CaptureUploadCoordinatorHandlers<TResult>,
    options: CaptureUploadCoordinatorOptions = {},
  ) {
    this.spool = spool;
    this.handlers = handlers;
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    const cancelSchedule = options.cancelSchedule ?? clearTimeout;
    this.cancelSchedule = (handle) => cancelSchedule(handle);
    this.maxConcurrentUploads = Math.max(
      1,
      Math.min(
        CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS,
        Math.floor(options.maxConcurrentUploads ?? 1),
      ),
    );
  }

  start(partition: string): void {
    this.stop();
    this.partition = partition;
    this.running = true;
    this.generation += 1;
    this.kick();
  }

  stop(): void {
    this.running = false;
    this.generation += 1;
    this.kickRequested = false;
    for (const controller of this.activeAborts) controller.abort();
    this.activeAborts.clear();
    this.lockAbort?.abort();
    this.lockAbort = null;
    if (this.timer !== null) this.cancelSchedule(this.timer);
    this.timer = null;
    this.partition = null;
  }

  kick(): void {
    if (!this.running || !this.partition) return;
    if (this.draining) {
      this.kickRequested = true;
      return;
    }
    if (this.timer !== null) {
      this.cancelSchedule(this.timer);
      this.timer = null;
    }
    const generation = this.generation;
    const partition = this.partition;
    this.draining = true;
    this.kickRequested = false;
    void this.drainWithLease(generation, partition)
      .catch((error: unknown) => {
        this.handlers.onError?.(error, partition);
        this.wait(captureRetryDelay(1), generation);
      })
      .finally(() => {
        this.draining = false;
        this.notifyIdleWaiters();
        this.handlers.onIdle?.();
        if (
          this.running &&
          (this.kickRequested || generation !== this.generation)
        ) {
          this.kickRequested = false;
          this.kick();
        }
      });
  }

  private wait(delayMs: number, generation: number): void {
    if (!this.running || generation !== this.generation) return;
    if (this.timer !== null) this.cancelSchedule(this.timer);
    this.timer = this.schedule(() => {
      this.timer = null;
      this.kick();
    }, Math.max(1, delayMs));
  }

  private async drain(generation: number): Promise<void> {
    const claimed = new Map<number, CaptureSpoolItem>();
    const deferred = new Set<number>();
    const outcomes = new Map<number, PromiseSettledResult<TResult>>();
    const inFlight = new Map<number, Promise<void>>();
    const canContinue = () =>
      this.running && generation === this.generation && this.partition !== null;

    const startUpload = (item: CaptureSpoolItem, ordinal: number): void => {
      const controller = new AbortController();
      const startedAtMs = this.now();
      this.activeAborts.add(controller);
      this.inFlightCount += 1;
      this.handlers.onAttempt?.(item);
      const pending = (async () => {
        try {
          const result = await this.uploadWithAbort(item, controller);
          outcomes.set(ordinal, { status: "fulfilled", value: result });
          // Store the receipt as soon as this request settles. A slower earlier
          // ordinal must not force this successfully-uploaded item to be sent
          // again while the rolling window keeps admitting later work.
          this.remoteAcknowledgements.set(ordinal, result);
        } catch (error: unknown) {
          outcomes.set(ordinal, { status: "rejected", reason: error });
        } finally {
          this.activeAborts.delete(controller);
          this.inFlightCount = Math.max(0, this.inFlightCount - 1);
          this.handlers.onSettled?.(
            item,
            Math.max(0, this.now() - startedAtMs),
          );
          this.notifyIdleWaiters();
        }
      })();
      inFlight.set(ordinal, pending);
      void pending.then(() => {
        if (inFlight.get(ordinal) === pending) inFlight.delete(ordinal);
      });
    };

    const commitReady = async (): Promise<"continue" | "stop"> => {
      while (claimed.size > 0) {
        const item = [...claimed.values()].sort(
          (left, right) => (left.ordinal as number) - (right.ordinal as number),
        )[0];
        const ordinal = item.ordinal as number;
        const outcome = outcomes.get(ordinal) ?? (
          this.remoteAcknowledgements.has(ordinal)
            ? {
                status: "fulfilled" as const,
                value: this.remoteAcknowledgements.get(ordinal) as TResult,
              }
            : undefined
        );
        if (!outcome) return "continue";
        if (outcome.status === "fulfilled") {
          let valid = true;
          try {
            valid = this.handlers.isAcknowledgementValid?.(item, outcome.value) ?? true;
          } catch {
            valid = false;
          }
          if (valid) {
            if (!(await this.acknowledge(item, outcome.value, generation))) {
              return "stop";
            }
            claimed.delete(ordinal);
            outcomes.delete(ordinal);
            continue;
          }
          // An invalid receipt is not a remote success. Do not retain it in
          // the replay cache: the same idempotency key must be retried.
          this.remoteAcknowledgements.delete(ordinal);
        }
        const error = outcome.status === "rejected"
          ? outcome.reason
          : new CaptureUploadReceiptError();
        // A non-retryable HTTP error is still not a durable receipt. Keep the
        // spool item and retry it with backoff; deleting it here would advance
        // the local ack cursor without a real 2xx durable admission.
        const retryCount = item.retryCount + 1;
        const requestedDelay = this.handlers.retryDelay?.(error, retryCount);
        const retryDelay =
          typeof requestedDelay === "number" &&
          Number.isFinite(requestedDelay) &&
          requestedDelay > 0
            ? requestedDelay
            : captureRetryDelay(retryCount);
        const nextAttemptAtMs = this.now() + retryDelay;
        await this.spool.markRetry(ordinal, retryCount, nextAttemptAtMs);
        this.handlers.onRetry?.(item, retryCount, error, nextAttemptAtMs);
        // A retryable head must preserve ordered local acknowledgement, but it
        // must not idle the whole partition. Remove only the failed attempt
        // from the rolling window; later ordinals may upload once and keep
        // their remote receipts until this head becomes committable.
        claimed.delete(ordinal);
        deferred.delete(ordinal);
        outcomes.delete(ordinal);
        this.wait(nextAttemptAtMs - this.now(), generation);
        return "continue";
      }
      return "continue";
    };

    while (canContinue()) {
      const beforeAdmission = await commitReady();
      if (beforeAdmission !== "continue") {
        return;
      }
      if (!canContinue() || !this.partition) return;

      const now = this.now();
      const scanLimit = Math.max(
        this.maxConcurrentUploads,
        claimed.size + this.maxConcurrentUploads,
      );
      const { items, snapshot } = await this.peekBatch(
        this.partition,
        now,
        scanLimit,
      );
      if (
        !canContinue() ||
        !this.partition ||
        items.some((item) => item.partition !== this.partition)
      ) {
        return;
      }
      this.handlers.onSnapshot?.(snapshot, this.partition);
      if (snapshot.expired > 0) {
        this.handlers.onExpired?.(snapshot.expired, this.partition);
      }

      let wakeAtMs: number | null = null;
      let admitted = false;
      for (const item of items) {
        if (item.ordinal === undefined) {
          throw new Error("durable capture item is missing its ordinal");
        }
        const ordinal = item.ordinal;
        if (claimed.has(ordinal) && !deferred.has(ordinal)) continue;
        claimed.set(ordinal, item);
        if (this.remoteAcknowledgements.has(ordinal)) {
          deferred.delete(ordinal);
          outcomes.set(ordinal, {
            status: "fulfilled",
            value: this.remoteAcknowledgements.get(ordinal) as TResult,
          });
          admitted = true;
          continue;
        }
        if (item.nextAttemptAtMs > now) {
          deferred.add(ordinal);
          wakeAtMs = wakeAtMs === null
            ? item.nextAttemptAtMs
            : Math.min(wakeAtMs, item.nextAttemptAtMs);
          continue;
        }
        const pauseUntil = this.handlers.beforeAttempt?.(item) ?? null;
        if (pauseUntil !== null && pauseUntil > now) {
          deferred.add(ordinal);
          wakeAtMs = wakeAtMs === null
            ? pauseUntil
            : Math.min(wakeAtMs, pauseUntil);
          continue;
        }
        if (inFlight.size >= this.maxConcurrentUploads) {
          if (!deferred.has(ordinal)) claimed.delete(ordinal);
          break;
        }
        deferred.delete(ordinal);
        startUpload(item, ordinal);
        admitted = true;
      }

      const afterAdmission = await commitReady();
      if (afterAdmission !== "continue") {
        return;
      }
      if (!canContinue()) return;
      if (inFlight.size > 0) {
        await Promise.race([...inFlight.values()]);
        continue;
      }
      if (wakeAtMs !== null) {
        this.wait(wakeAtMs - this.now(), generation);
        return;
      }
      if (items.length === 0 || !admitted) return;
    }
  }

  private async peekBatch(
    partition: string,
    nowMs: number,
    limit = this.maxConcurrentUploads,
  ): Promise<{ items: CaptureSpoolItem[]; snapshot: CaptureSpoolSnapshot }> {
    if (this.spool.peekBatch) {
      return this.spool.peekBatch(partition, limit, nowMs);
    }
    const { item, snapshot } = await this.spool.peek(partition, nowMs);
    return { items: item ? [item] : [], snapshot };
  }

  private async drainWithLease(
    generation: number,
    partition: string,
  ): Promise<void> {
    const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
    if (!locks) {
      await this.drain(generation);
      return;
    }
    const controller = new AbortController();
    this.lockAbort = controller;
    try {
      await locks.request(
        `echodesk:capture-upload:${partition}`,
        { mode: "exclusive", signal: controller.signal },
        async () => {
          if (
            !this.running ||
            generation !== this.generation ||
            this.partition !== partition
          ) {
            return;
          }
          await this.drain(generation);
        },
      );
    } catch (error: unknown) {
      if (!controller.signal.aborted) throw error;
    } finally {
      if (this.lockAbort === controller) this.lockAbort = null;
    }
  }

  isIdle(): boolean {
    return !this.draining && this.inFlightCount === 0;
  }

  async awaitIdle(timeoutMs: number): Promise<void> {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new Error("formal capture idle timeout must be positive");
    }
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const waiter = () => {
        if (settled) return;
        settled = true;
        this.idleWaiters.delete(waiter);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        this.idleWaiters.delete(waiter);
        reject(new Error("formal capture partition idle timed out"));
      }, timeoutMs);
      this.idleWaiters.add(waiter);
      this.notifyIdleWaiters();
    });
  }

  private notifyIdleWaiters(): void {
    if (!this.isIdle()) return;
    for (const waiter of [...this.idleWaiters]) waiter();
  }

  private uploadWithAbort(
    item: CaptureSpoolItem,
    controller: AbortController,
  ): Promise<TResult> {
    return new Promise<TResult>((resolve, reject) => {
      const onAbort = () => {
        reject(controller.signal.reason ?? new DOMException("aborted", "AbortError"));
      };
      if (controller.signal.aborted) {
        onAbort();
        return;
      }
      controller.signal.addEventListener("abort", onAbort, { once: true });
      void this.handlers
        .upload(item, controller.signal)
        .then(resolve, reject)
        .finally(() => {
          controller.signal.removeEventListener("abort", onAbort);
        });
    });
  }

  private async acknowledge(
    item: CaptureSpoolItem,
    result: TResult,
    generation: number,
  ): Promise<boolean> {
    if (item.ordinal === undefined) return false;
    try {
      await this.spool.acknowledge(item.ordinal);
    } catch (error: unknown) {
      this.handlers.onError?.(error, item.partition);
      this.wait(captureRetryDelay(1), generation);
      return false;
    }
    this.remoteAcknowledgements.delete(item.ordinal);
    if (!this.running || generation !== this.generation) return false;
    try {
      this.handlers.onAcknowledged?.(item, result);
    } catch (error: unknown) {
      this.handlers.onError?.(error, item.partition);
    }
    return true;
  }

}

export interface CaptureUploadPoolHandlers<TResult = void>
  extends CaptureUploadCoordinatorHandlers<TResult> {
  isPartitionEligible?(
    partition: CaptureSpoolPartitionSummary,
  ): boolean | Promise<boolean>;
  onInventory?(inventory: CaptureSpoolInventory): void;
  onPoolActivity?(activity: CaptureUploadPoolActivity): void;
}

export interface CaptureUploadPoolActivity {
  activeInFlightCurrent: number;
  activeInFlightMax: number;
  recoveryInFlightCurrent: number;
  recoveryInFlightMax: number;
  globalInFlightCurrent: number;
  globalInFlightMax: number;
  attemptCount: number;
  acknowledgedCount: number;
  completedRequestCount: number;
  requestLatencyMsSum: number;
  requestLatencyMsMax: number;
}

class FairRequestPermits {
  private readonly capacity: number;
  private inUse = 0;
  private readonly waiters: Array<{
    signal: AbortSignal;
    resolve: (release: () => void) => void;
    reject: (error: unknown) => void;
    onAbort: () => void;
  }> = [];

  constructor(capacity: number) {
    this.capacity = capacity;
  }

  async run<T>(signal: AbortSignal, work: () => Promise<T>): Promise<T> {
    const release = await this.acquire(signal);
    try {
      return await work();
    } finally {
      release();
    }
  }

  private acquire(signal: AbortSignal): Promise<() => void> {
    if (signal.aborted) return Promise.reject(signal.reason);
    return new Promise((resolve, reject) => {
      const waiter = {
        signal,
        resolve,
        reject,
        onAbort: () => {
          const index = this.waiters.indexOf(waiter);
          if (index >= 0) this.waiters.splice(index, 1);
          reject(signal.reason);
        },
      };
      signal.addEventListener("abort", waiter.onAbort, { once: true });
      this.waiters.push(waiter);
      this.drain();
    });
  }

  private drain(): void {
    while (this.inUse < this.capacity && this.waiters.length > 0) {
      const waiter = this.waiters.shift();
      if (!waiter || waiter.signal.aborted) continue;
      waiter.signal.removeEventListener("abort", waiter.onAbort);
      this.inUse += 1;
      let released = false;
      waiter.resolve(() => {
        if (released) return;
        released = true;
        this.inUse = Math.max(0, this.inUse - 1);
        this.drain();
      });
    }
  }
}

export interface CaptureUploadPoolOptions
  extends CaptureUploadCoordinatorOptions {
  maxParallelRequests?: number;
}

/**
 * 实时分区固定保留三个请求槽；恢复分区通过公平 permit 队列共享剩余一槽。
 * 单一恢复分区只占用一槽，多分区的后续批次排到队尾，避免恢复饿死实时上传。
 * HTTP 可并发，但每个 consumer 仍在同一 Web Lock 内按 ordinal 提交。
 */
export class CaptureUploadPool<TResult = void> {
  private readonly spool: CaptureUploadSpool;
  private readonly handlers: CaptureUploadPoolHandlers<TResult>;
  private readonly options: CaptureUploadPoolOptions;
  private readonly now: () => number;
  private readonly schedule: NonNullable<CaptureUploadCoordinatorOptions["schedule"]>;
  private readonly cancelSchedule: NonNullable<CaptureUploadCoordinatorOptions["cancelSchedule"]>;
  private readonly maxParallelRequests: number;
  private readonly activePartitionConcurrency: number;
  private readonly recoveryPartitionConcurrency: number;
  private readonly recoveryRequestConcurrency: number;
  private readonly recoveryPermits: FairRequestPermits;
  private readonly globalPermits: FairRequestPermits;
  /** 同一 durable operation 在 active/recovery coordinator 之间只允许一次真实 HTTP。 */
  private readonly inFlightOperations = new Map<string, Promise<TResult>>();
  private readonly coordinators = new Map<
    string,
    CaptureUploadCoordinator<TResult>
  >();
  private activePartition: string | null = null;
  private running = false;
  private generation = 0;
  private rebalancing = false;
  private rebalanceRequested = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly partitionIdleWaiters = new Set<{
    partition: string;
    resolve: () => void;
    reject: (error: unknown) => void;
    timer: ReturnType<typeof setTimeout>;
  }>();
  private activity: CaptureUploadPoolActivity = {
    activeInFlightCurrent: 0,
    activeInFlightMax: 0,
    recoveryInFlightCurrent: 0,
    recoveryInFlightMax: 0,
    globalInFlightCurrent: 0,
    globalInFlightMax: 0,
    attemptCount: 0,
    acknowledgedCount: 0,
    completedRequestCount: 0,
    requestLatencyMsSum: 0,
    requestLatencyMsMax: 0,
  };

  constructor(
    spool: CaptureUploadSpool,
    handlers: CaptureUploadPoolHandlers<TResult>,
    options: CaptureUploadPoolOptions = {},
  ) {
    this.spool = spool;
    this.handlers = handlers;
    this.options = options;
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    const cancelSchedule = options.cancelSchedule ?? clearTimeout;
    this.cancelSchedule = (handle) => cancelSchedule(handle);
    this.maxParallelRequests = Math.max(
      1,
      Math.min(
        CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS,
        options.maxParallelRequests ??
          CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS,
      ),
    );
    this.activePartitionConcurrency = Math.min(
      CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
      this.maxParallelRequests,
    );
    this.recoveryRequestConcurrency = Math.max(
      0,
      this.maxParallelRequests - this.activePartitionConcurrency,
    );
    this.recoveryPartitionConcurrency = Math.min(
      CAPTURE_UPLOAD_RECOVERY_PARTITION_CONCURRENCY,
      this.recoveryRequestConcurrency,
    );
    this.recoveryPermits = new FairRequestPermits(
      this.recoveryRequestConcurrency,
    );
    this.globalPermits = new FairRequestPermits(this.maxParallelRequests);
  }

  /** 启动调度；首个实时 chunk 取得所有权前，只运行有界 recovery。 */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.generation += 1;
    this.publishActivity();
    this.kick();
  }

  /** 暂停网络工作，同时保留当前实时采集所有权。 */
  pause(): void {
    this.running = false;
    this.generation += 1;
    this.rebalanceRequested = false;
    if (this.timer !== null) this.cancelSchedule(this.timer);
    this.timer = null;
    for (const coordinator of this.coordinators.values()) coordinator.stop();
    this.coordinators.clear();
  }

  dispose(): void {
    this.pause();
    this.activePartition = null;
    this.publishActivity();
  }

  /**
   * 唯一所有权写入口。router 只能为已完成 durable 事务，且冻结 scope
   * 仍等于当前麦克风 scope 的 item 调用。
   */
  setActivePartition(partition: string): boolean {
    if (!partition) throw new Error("active capture partition is required");
    if (partition === this.activePartition) return false;
    this.activePartition = partition;
    this.generation += 1;
    for (const coordinator of this.coordinators.values()) coordinator.stop();
    this.coordinators.clear();
    this.publishActivity();
    if (this.running) this.requestRebalance();
    return true;
  }

  currentActivePartition(): string | null {
    return this.activePartition;
  }

  isActivePartition(partition: string): boolean {
    return partition === this.activePartition;
  }

  kick(): void {
    if (!this.running) return;
    if (this.activePartition) {
      this.coordinators.get(this.activePartition)?.kick();
    }
    this.requestRebalance();
  }

  private requestRebalance(): void {
    if (!this.running) return;
    if (this.timer !== null) {
      this.cancelSchedule(this.timer);
      this.timer = null;
    }
    if (this.rebalancing) {
      this.rebalanceRequested = true;
      return;
    }
    const generation = this.generation;
    this.rebalancing = true;
    this.rebalanceRequested = false;
    void this.rebalance(generation)
      .catch((error: unknown) => {
        this.handlers.onError?.(error, this.activePartition ?? undefined);
        this.wait(captureRetryDelay(1), generation);
      })
      .finally(() => {
        this.rebalancing = false;
        if (
          this.running &&
          (this.rebalanceRequested || generation !== this.generation)
        ) {
          this.rebalanceRequested = false;
          this.requestRebalance();
        }
      });
  }

  private async rebalance(generation: number): Promise<void> {
    const activePartition = this.activePartition;
    if (!this.running) return;
    const now = this.now();
    const inventory = await this.spool.listPartitions(now);
    if (
      !this.running ||
      generation !== this.generation ||
      activePartition !== this.activePartition
    ) {
      return;
    }
    this.handlers.onInventory?.(inventory);
    this.notifyPartitionIdleWaiters();
    if (inventory.expired > 0) {
      this.handlers.onExpired?.(inventory.expired, activePartition ?? undefined);
    }

    const eligibility = await Promise.all(
      inventory.partitions.map(async (partition) => ({
        partition,
        eligible:
          partition.partition === activePartition ||
          (await (this.handlers.isPartitionEligible?.(partition) ?? true)),
      })),
    );
    if (
      !this.running ||
      generation !== this.generation ||
      activePartition !== this.activePartition
    ) {
      return;
    }

    const eligibleRecovery = eligibility
      .filter(
        ({ partition, eligible }) =>
          eligible &&
          partition.partition !== activePartition,
      )
      .map(({ partition }) => partition)
      .sort((left, right) => left.oldestOrdinal - right.oldestOrdinal);
    const desired = new Set<string>([
      ...(activePartition ? [activePartition] : []),
      ...(this.recoveryRequestConcurrency > 0
        ? eligibleRecovery.map((partition) => partition.partition)
        : []),
    ]);

    for (const [partition, coordinator] of this.coordinators) {
      if (desired.has(partition)) continue;
      coordinator.stop();
      this.coordinators.delete(partition);
    }
    for (const partition of desired) {
      let coordinator = this.coordinators.get(partition);
      if (!coordinator) {
        coordinator = this.createCoordinator(
          partition,
          partition === activePartition ? "active" : "recovery",
        );
        this.coordinators.set(partition, coordinator);
        coordinator.start(partition);
      } else {
        coordinator.kick();
      }
    }
    this.notifyPartitionIdleWaiters();

    const futureRecovery = eligibility
      .filter(
        ({ partition, eligible }) =>
          eligible &&
          partition.partition !== activePartition &&
          partition.nextAttemptAtMs > now,
      )
      .map(({ partition }) => partition.nextAttemptAtMs);
    if (futureRecovery.length > 0) {
      this.wait(Math.min(...futureRecovery) - now, generation);
    }
  }

  private createCoordinator(
    partition: string,
    role: "active" | "recovery",
  ): CaptureUploadCoordinator<TResult> {
    const releaseRecovery = (): void => {
      if (partition === this.activePartition) return;
      queueMicrotask(() => {
        const coordinator = this.coordinators.get(partition);
        if (!coordinator || partition === this.activePartition) return;
        coordinator.stop();
        this.coordinators.delete(partition);
        this.requestRebalance();
      });
    };
    return new CaptureUploadCoordinator<TResult>(
      this.spool,
      {
        upload: (item, signal) =>
          this.executeTrackedUpload(role, item, signal),
        isAcknowledgementValid: this.handlers.isAcknowledgementValid,
        beforeAttempt: this.handlers.beforeAttempt,
        retryDelay: this.handlers.retryDelay,
        shouldRetry: this.handlers.shouldRetry,
        onAcknowledged: (item, result) => {
          this.activity.acknowledgedCount += 1;
          this.publishActivity();
          this.handlers.onAcknowledged?.(item, result);
        },
        onRetry: (item, retryCount, error, nextAttemptAtMs) => {
          this.handlers.onRetry?.(
            item,
            retryCount,
            error,
            nextAttemptAtMs,
          );
          this.requestRebalance();
        },
        onDiscarded: (item, error) => {
          this.handlers.onDiscarded?.(item, error);
          this.requestRebalance();
        },
        onSnapshot: (snapshot) => {
          this.handlers.onSnapshot?.(snapshot, partition);
          if (snapshot.depth === 0) releaseRecovery();
        },
        onIdle: () => this.notifyPartitionIdleWaiters(),
        onExpired: (count) => this.handlers.onExpired?.(count, partition),
        onError: (error) => this.handlers.onError?.(error, partition),
      },
      {
        now: this.options.now,
        schedule: this.options.schedule,
        cancelSchedule: this.options.cancelSchedule,
        maxConcurrentUploads:
          role === "active"
            ? this.activePartitionConcurrency
            : this.recoveryPartitionConcurrency,
      },
    );
  }

  private executeTrackedUpload(
    role: "active" | "recovery",
    item: CaptureSpoolItem,
    signal: AbortSignal,
  ): Promise<TResult> {
    const upload = async (): Promise<TResult> => {
      const startedAtMs = this.now();
      this.recordAttempt(role);
      this.handlers.onAttempt?.(item);
      try {
        return await this.handlers.upload(item, signal);
      } finally {
        const latencyMs = Math.max(0, this.now() - startedAtMs);
        this.recordSettled(latencyMs, role);
        this.handlers.onSettled?.(item, latencyMs);
      }
    };
    const existing = this.inFlightOperations.get(item.idempotencyKey);
    if (existing) return this.awaitWithAbort(existing, signal);

    const request = this.globalPermits.run(
      signal,
      () => role === "recovery"
        ? this.recoveryPermits.run(signal, upload)
        : upload(),
    );
    this.inFlightOperations.set(item.idempotencyKey, request);
    void request.then(
      () => this.releaseInFlightOperation(item.idempotencyKey, request),
      () => this.releaseInFlightOperation(item.idempotencyKey, request),
    );
    return request;
  }

  private releaseInFlightOperation(
    idempotencyKey: string,
    request: Promise<TResult>,
  ): void {
    if (this.inFlightOperations.get(idempotencyKey) === request) {
      this.inFlightOperations.delete(idempotencyKey);
    }
  }

  private awaitWithAbort(
    pending: Promise<TResult>,
    signal: AbortSignal,
  ): Promise<TResult> {
    if (signal.aborted) return Promise.reject(signal.reason);
    return new Promise<TResult>((resolve, reject) => {
      const onAbort = () => {
        cleanup();
        reject(signal.reason ?? new DOMException("aborted", "AbortError"));
      };
      const cleanup = () => signal.removeEventListener("abort", onAbort);
      signal.addEventListener("abort", onAbort, { once: true });
      pending.then(
        (value) => {
          cleanup();
          resolve(value);
        },
        (error: unknown) => {
          cleanup();
          reject(error);
        },
      );
    });
  }

  private recordAttempt(role: "active" | "recovery"): void {
    this.activity.globalInFlightCurrent += 1;
    if (role === "active") this.activity.activeInFlightCurrent += 1;
    else this.activity.recoveryInFlightCurrent += 1;
    this.activity.attemptCount += 1;
    this.activity.globalInFlightMax = Math.max(
      this.activity.globalInFlightMax,
      this.activity.globalInFlightCurrent,
    );
    this.publishActivity();
  }

  private recordSettled(
    latencyMs: number,
    role: "active" | "recovery",
  ): void {
    this.activity.globalInFlightCurrent = Math.max(
      0,
      this.activity.globalInFlightCurrent - 1,
    );
    if (role === "active") {
      this.activity.activeInFlightCurrent = Math.max(
        0,
        this.activity.activeInFlightCurrent - 1,
      );
    } else {
      this.activity.recoveryInFlightCurrent = Math.max(
        0,
        this.activity.recoveryInFlightCurrent - 1,
      );
    }
    this.activity.completedRequestCount += 1;
    this.activity.requestLatencyMsSum += latencyMs;
    this.activity.requestLatencyMsMax = Math.max(
      this.activity.requestLatencyMsMax,
      latencyMs,
    );
    this.publishActivity();
  }

  private publishActivity(): void {
    this.activity.activeInFlightMax = Math.max(
      this.activity.activeInFlightMax,
      this.activity.activeInFlightCurrent,
    );
    this.activity.recoveryInFlightMax = Math.max(
      this.activity.recoveryInFlightMax,
      this.activity.recoveryInFlightCurrent,
    );
    this.handlers.onPoolActivity?.({ ...this.activity });
    this.notifyPartitionIdleWaiters();
  }

  async awaitPartitionIdle(partition: string, timeoutMs = 120_000): Promise<void> {
    if (!partition) throw new Error("formal capture partition is required");
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new Error("formal capture idle timeout must be positive");
    }
    return new Promise<void>((resolve, reject) => {
      const waiter = {
        partition,
        resolve: () => {
          this.partitionIdleWaiters.delete(waiter);
          clearTimeout(waiter.timer);
          resolve();
        },
        reject: (error: unknown) => {
          this.partitionIdleWaiters.delete(waiter);
          clearTimeout(waiter.timer);
          reject(error);
        },
        timer: undefined as unknown as ReturnType<typeof setTimeout>,
      };
      waiter.timer = setTimeout(
        () => waiter.reject(new Error("formal capture partition idle timed out")),
        timeoutMs,
      );
      this.partitionIdleWaiters.add(waiter);
      this.notifyPartitionIdleWaiters();
    });
  }

  cancelPartitionIdle(
    partition: string,
    reason = "formal meeting became terminal",
  ): void {
    for (const waiter of [...this.partitionIdleWaiters]) {
      if (waiter.partition === partition) waiter.reject(new Error(reason));
    }
  }

  private notifyPartitionIdleWaiters(): void {
    for (const waiter of [...this.partitionIdleWaiters]) {
      void this.checkPartitionIdle(waiter);
    }
  }

  private async checkPartitionIdle(
    waiter: {
      partition: string;
      resolve: () => void;
      reject: (error: unknown) => void;
      timer: ReturnType<typeof setTimeout>;
    },
  ): Promise<void> {
    if (!this.partitionIdleWaiters.has(waiter)) return;
    const snapshot = await this.spool.snapshot(waiter.partition, this.now());
    const coordinator = this.coordinators.get(waiter.partition);
    if (
      snapshot.depth === 0 &&
      (!coordinator || coordinator.isIdle())
    ) {
      waiter.resolve();
    }
  }

  private wait(delayMs: number, generation: number): void {
    if (!this.running || generation !== this.generation) return;
    if (this.timer !== null) this.cancelSchedule(this.timer);
    this.timer = this.schedule(() => {
      this.timer = null;
      this.requestRebalance();
    }, Math.max(1, delayMs));
  }
}
