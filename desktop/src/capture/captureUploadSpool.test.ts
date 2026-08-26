import assert from "node:assert/strict";
import test from "node:test";

import {
  CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
  CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS,
  CAPTURE_UPLOAD_RECOVERY_PARTITION_CONCURRENCY,
  CAPTURE_SPOOL_ACTIVE_RESERVED_BYTES,
  CAPTURE_SPOOL_ACTIVE_RESERVED_ITEMS,
  CAPTURE_SPOOL_GLOBAL_MAX_BYTES,
  CAPTURE_SPOOL_MAX_BYTES,
  CAPTURE_SPOOL_GLOBAL_MAX_ITEMS,
  CAPTURE_SPOOL_MAX_ITEMS,
  CAPTURE_SPOOL_TTL_MS,
  captureUploadRetryDelay,
  MemoryCaptureUploadSpool,
  captureSpoolLegacyPartition,
  captureSpoolPartition,
  isCaptureSpoolPartitionCompatible,
  isCaptureSpoolHardCapacityRejection,
  type CaptureSpoolItem,
  type CaptureUploadSpool,
} from "./captureUploadSpool.ts";
import {
  CaptureUploadCoordinator,
  CaptureUploadPool,
  type CaptureUploadPoolActivity,
} from "./captureUploadCoordinator.ts";

const TEST_WAV = new Blob([new Uint8Array(4)]);

interface TestCaptureReceipt {
  admission: {
    status: "accepted" | "pending" | "rejected" | "unknown";
    durable: boolean;
    receipt_id: string | null;
  };
  device_id: string | null;
  capture_session_id: string | null;
  source: string | null;
}

function item(
  sequence: number,
  options: {
    origin?: string;
    deviceId?: string;
    principalScope?: string;
    captureSessionId?: string;
    bytes?: number;
    capturedAtMs?: number;
    meetingId?: string | null;
  } = {},
): CaptureSpoolItem {
  const origin = options.origin ?? "https://capture.example";
  const byteSize = options.bytes ?? 4;
  const capturedAtMs = options.capturedAtMs ?? 1_000;
  const deviceId = options.deviceId ?? "device-local";
  const principalScope = options.principalScope ?? "scope-local";
  const captureSessionId = options.captureSessionId ?? "capture-session";
  return {
    partition: captureSpoolPartition(
      origin,
      principalScope,
      deviceId,
      captureSessionId,
    ),
    origin,
    principalScope,
    wav: byteSize === TEST_WAV.size
      ? TEST_WAV
      : new Blob([new Uint8Array(byteSize)]),
    byteSize,
    capturedAtMs,
    expiresAtMs: capturedAtMs + CAPTURE_SPOOL_TTL_MS,
    meetingId: options.meetingId ?? null,
    scope: {
      deviceId,
      captureSessionId,
      source: "desktop",
    },
    segmentId: `segment-${sequence}`,
    idempotencyKey: `capture-${sequence}`,
    retryCount: 0,
    nextAttemptAtMs: capturedAtMs,
  };
}

function durableReceipt(queued: CaptureSpoolItem): TestCaptureReceipt {
  return {
    admission: {
      status: "accepted",
      durable: true,
      receipt_id: `capture-${queued.idempotencyKey}`,
    },
    device_id: queued.scope.deviceId,
    capture_session_id: queued.scope.captureSessionId ?? null,
    source: queued.scope.source,
  };
}

function isDurableReceipt(
  queued: CaptureSpoolItem,
  receipt: TestCaptureReceipt,
): boolean {
  return (
    receipt.admission.status === "accepted" &&
    receipt.admission.durable === true &&
    receipt.admission.receipt_id !== null &&
    receipt.device_id === queued.scope.deviceId &&
    receipt.capture_session_id === (queued.scope.captureSessionId ?? null) &&
    receipt.source === queued.scope.source
  );
}

test("v2 partition 对同一 capture session 稳定并隔离同设备的不同 session", () => {
  const first = item(1, { captureSessionId: "session-a" });
  const sameSession = item(2, { captureSessionId: "session-a" });
  const nextSession = item(3, { captureSessionId: "session-b" });

  assert.equal(first.partition, sameSession.partition);
  assert.notEqual(first.partition, nextSession.partition);
  assert.equal(
    isCaptureSpoolPartitionCompatible(
      first.partition,
      first.origin,
      first.principalScope,
      first.scope.deviceId,
      first.scope.captureSessionId,
    ),
    true,
  );
});

test("formal lane 与 free backlog 隔离，但仍通过同一 scope fence recovery", async () => {
  const free = item(1, { capturedAtMs: 2_000 });
  const formal = item(2, { capturedAtMs: 2_000, meetingId: "meeting-formal" });
  formal.partition = captureSpoolPartition(
    formal.origin,
    formal.principalScope,
    formal.scope.deviceId,
    formal.scope.captureSessionId ?? "",
    "formal",
  );

  assert.notEqual(free.partition, formal.partition);
  assert.equal(
    isCaptureSpoolPartitionCompatible(
      formal.partition,
      formal.origin,
      formal.principalScope,
      formal.scope.deviceId,
      formal.scope.captureSessionId,
    ),
    true,
  );

  const spool = new MemoryCaptureUploadSpool();
  for (let index = 0; index < 24; index += 1) {
    await spool.enqueue(item(100 + index, { capturedAtMs: 2_000 }));
  }
  await spool.enqueue(formal, 2_000);

  const attempts: string[] = [];
  let release = () => undefined;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  const pool = new CaptureUploadPool(spool, {
    upload: async (queued, signal) => {
      attempts.push(queued.segmentId);
      await new Promise<void>((resolve, reject) => {
        const onAbort = () => reject(signal.reason);
        signal.addEventListener("abort", onAbort, { once: true });
        blocked.then(() => {
          signal.removeEventListener("abort", onAbort);
          resolve();
        });
      });
    },
    isPartitionEligible: (summary) =>
      summary.origin === formal.origin &&
      summary.principalScope === formal.principalScope &&
      summary.deviceId === formal.scope.deviceId &&
      isCaptureSpoolPartitionCompatible(
        summary.partition,
        summary.origin,
        summary.principalScope,
        summary.deviceId,
        summary.captureSessionId,
      ),
  }, { now: () => 2_000 });
  pool.setActivePartition(formal.partition);
  pool.start();

  await eventually(() => attempts.includes(formal.segmentId));
  assert.equal(attempts.includes(formal.segmentId), true);
  release();
  pool.dispose();
});

test("旧 legacy key 在 generation cutover 后不再进入 recovery", async () => {
  const current = item(1);
  const legacy: CaptureSpoolItem = {
    ...current,
    ordinal: 1,
    partition: captureSpoolLegacyPartition(
      current.origin,
      current.principalScope,
      current.scope.deviceId,
    ),
    scope: { deviceId: current.scope.deviceId, source: "desktop" },
  };
  const spool = new MemoryCaptureUploadSpool([legacy]);
  const inventory = await spool.listPartitions(1_000);
  const fresh = item(2, { captureSessionId: "post-cutover-session" });
  await spool.enqueue(fresh, 1_000);
  const replayed: string[] = [];
  const pool = new CaptureUploadPool(spool, {
    upload: async (queued) => replayed.push(queued.segmentId),
    isPartitionEligible: (summary) =>
      isCaptureSpoolPartitionCompatible(
        summary.partition,
        summary.origin,
        summary.principalScope,
        summary.deviceId,
        summary.captureSessionId,
      ),
  }, { now: () => 1_000 });

  assert.equal(inventory.partitions.length, 1);
  assert.equal(inventory.partitions[0]?.captureSessionId, undefined);
  assert.equal(
    isCaptureSpoolPartitionCompatible(
      legacy.partition,
      legacy.origin,
      legacy.principalScope,
      legacy.scope.deviceId,
      legacy.scope.captureSessionId,
    ),
    false,
  );
  pool.setActivePartition(fresh.partition);
  pool.start();
  await eventually(() => replayed.includes(fresh.segmentId));
  pool.dispose();
  assert.deepEqual(replayed, [fresh.segmentId]);
  assert.equal((await spool.snapshot(legacy.partition, 1_000)).depth, 1);
});

test("已满 legacy 分区不阻塞同设备新 session，而新 session 自身满时仍拒绝", async () => {
  const legacyTemplate = item(1, { captureSessionId: "legacy-session" });
  const legacyPartition = captureSpoolLegacyPartition(
    legacyTemplate.origin,
    legacyTemplate.principalScope,
    legacyTemplate.scope.deviceId,
  );
  const legacySeed = Array.from(
    { length: CAPTURE_SPOOL_MAX_ITEMS },
    (_, index) => ({
      ...item(index, { captureSessionId: "legacy-session" }),
      ordinal: index + 1,
      partition: legacyPartition,
    }),
  );
  const newSession = item(20_000, { captureSessionId: "new-session" });
  const legacyFullSpool = new MemoryCaptureUploadSpool(legacySeed);
  const admitted = await legacyFullSpool.enqueue(newSession, 1_000, {
    activePartition: newSession.partition,
  });

  assert.equal(admitted.accepted, true);
  assert.equal(admitted.snapshot.depth, 1);
  assert.equal(admitted.snapshot.globalDepth, CAPTURE_SPOOL_MAX_ITEMS + 1);
  assert.equal(admitted.snapshot.partitionCount, 2);

  const currentSessionFull = new MemoryCaptureUploadSpool(
    Array.from({ length: CAPTURE_SPOOL_MAX_ITEMS }, (_, index) => ({
      ...item(index, { captureSessionId: "new-session" }),
      ordinal: index + 1,
    })),
  );
  const rejected = await currentSessionFull.enqueue(
    item(30_000, { captureSessionId: "new-session" }),
    1_000,
  );
  assert.deepEqual(
    { accepted: rejected.accepted, reason: rejected.accepted ? null : rejected.reason },
    { accepted: false, reason: "count_limit" },
  );
});

async function eventually(predicate: () => boolean, attempts = 100): Promise<void> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error("condition did not settle");
}

class VirtualScheduler {
  private currentMs: number;
  private nextHandle = 1;
  private readonly events = new Map<
    number,
    { atMs: number; callback: () => void }
  >();

  constructor(startMs: number) {
    this.currentMs = startMs;
  }

  now = (): number => this.currentMs;

  schedule = (
    callback: () => void,
    delayMs: number,
  ): ReturnType<typeof setTimeout> => {
    const handle = this.nextHandle++;
    this.events.set(handle, {
      atMs: this.currentMs + Math.max(0, delayMs),
      callback,
    });
    return handle as unknown as ReturnType<typeof setTimeout>;
  };

  cancel = (handle: ReturnType<typeof setTimeout>): void => {
    this.events.delete(handle as unknown as number);
  };

  async runUntil(targetMs: number): Promise<void> {
    for (;;) {
      await new Promise((resolve) => setImmediate(resolve));
      const next = [...this.events.entries()]
        .filter(([, event]) => event.atMs <= targetMs)
        .sort(
          ([leftHandle, left], [rightHandle, right]) =>
            left.atMs - right.atMs || leftHandle - rightHandle,
        )[0];
      if (!next) {
        this.currentMs = targetMs;
        await new Promise((resolve) => setImmediate(resolve));
        return;
      }
      const [handle, event] = next;
      this.events.delete(handle);
      this.currentMs = event.atMs;
      event.callback();
    }
  }
}

test("有界 spool 分别拒绝条数已满和字节预算已满", async () => {
  const countSpool = new MemoryCaptureUploadSpool(
    Array.from({ length: CAPTURE_SPOOL_MAX_ITEMS }, (_, index) => ({
      ...item(index),
      ordinal: index + 1,
    })),
  );
  const countRejected = await countSpool.enqueue(item(99), 1_000);
  assert.deepEqual(
    { accepted: countRejected.accepted, reason: countRejected.accepted ? null : countRejected.reason },
    { accepted: false, reason: "count_limit" },
  );

  // Seed accounting directly so the test does not allocate a 1GiB Blob.
  const byteSpool = new MemoryCaptureUploadSpool([{
    ...item(1),
    byteSize: CAPTURE_SPOOL_MAX_BYTES,
    ordinal: 1,
  }]);
  const byteRejected = await byteSpool.enqueue(item(2, { bytes: 1 }), 1_000);
  assert.deepEqual(
    { accepted: byteRejected.accepted, reason: byteRejected.accepted ? null : byteRejected.reason },
    { accepted: false, reason: "byte_limit" },
  );
});

test("TTL 到期会显式计数并删除，释放条数和字节预算", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(1, { capturedAtMs: 5_000 });
  await spool.enqueue(queued, 5_000);

  const expired = await spool.peek(queued.partition, queued.expiresAtMs);
  assert.equal(expired.item, null);
  assert.deepEqual(expired.snapshot, {
    depth: 0,
    bytes: 0,
    globalDepth: 0,
    globalBytes: 0,
    partitionCount: 0,
    expired: 1,
  });

  const next = await spool.enqueue(item(2, { capturedAtMs: queued.expiresAtMs }), queued.expiresAtMs);
  assert.equal(next.accepted, true);
  assert.equal(next.snapshot.depth, 1);
});

test("10 分钟后的离线项在 24 小时内仍可恢复，超过 24 小时单独过期", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(7, { capturedAtMs: 1_000 });
  await spool.enqueue(queued, 1_000);

  const recoverable = await spool.peek(queued.partition, 1_000 + 10 * 60 * 1_000 + 1);
  assert.equal(recoverable.item?.segmentId, queued.segmentId);
  const expired = await spool.peek(queued.partition, 1_000 + 24 * 60 * 60 * 1_000 + 1);
  assert.equal(expired.item, null);
  assert.equal(expired.snapshot.expired, 1);
});

test("enqueue 拒绝与 origin/principal/device 不一致的伪造 partition", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const forged = item(1);
  forged.partition = captureSpoolPartition(
    forged.origin,
    forged.principalScope,
    "device-other",
    forged.scope.captureSessionId ?? "capture-session",
  );
  await assert.rejects(
    () => spool.enqueue(forged, 1_000),
    /capture spool item is invalid/,
  );
});

test("失败保留队首并用同一 segment/idempotency 重试，单 consumer 严格保序", async () => {
  let now = 10_000;
  const spool = new MemoryCaptureUploadSpool();
  const first = item(1, { capturedAtMs: now, meetingId: "meeting-a" });
  const second = item(2, { capturedAtMs: now, meetingId: "meeting-b" });
  await spool.enqueue(first, now);
  await spool.enqueue(second, now);

  const attempts: Array<[string, string, string | null]> = [];
  let failFirst = true;
  let concurrent = 0;
  let maxConcurrent = 0;
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (queued) => {
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        attempts.push([queued.segmentId, queued.idempotencyKey, queued.meetingId]);
        concurrent -= 1;
        if (queued.segmentId === first.segmentId && failFirst) {
          failFirst = false;
          throw new Error("transient");
        }
      },
    },
    {
      now: () => now,
      schedule: (callback, delayMs) => {
        now += delayMs;
        queueMicrotask(callback);
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(first.partition);
  await eventually(() => attempts.length === 3);
  coordinator.stop();

  assert.deepEqual(attempts, [
    ["segment-1", "capture-1", "meeting-a"],
    ["segment-1", "capture-1", "meeting-a"],
    ["segment-2", "capture-2", "meeting-b"],
  ]);
  assert.equal(maxConcurrent, 1);
  assert.equal((await spool.snapshot(first.partition, now)).depth, 0);
});

test("active partition 允许三个 HTTP 请求并发、持续补槽且只按 ordinal 顺序确认", async () => {
  const now = 12_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = Array.from({ length: 12 }, (_, index) =>
    item(index + 1, { capturedAtMs: now }),
  );
  for (const next of queued) await spool.enqueue(next, now);

  let concurrent = 0;
  let maxConcurrent = 0;
  const completions = new Map<string, () => void>();
  const acknowledged: string[] = [];
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: (queued) =>
        new Promise<void>((resolve) => {
          concurrent += 1;
          maxConcurrent = Math.max(maxConcurrent, concurrent);
          const finish = () => {
            concurrent -= 1;
            resolve();
          };
          completions.set(queued.segmentId, finish);
        }),
      onAcknowledged: (queued) => acknowledged.push(queued.segmentId),
    },
    {
      now: () => now,
      maxConcurrentUploads: CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
    },
  );
  coordinator.start(queued[0].partition);
  await eventually(
    () => maxConcurrent === CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
  );

  for (const next of queued.slice(1, 3)) {
    completions.get(next.segmentId)?.();
  }
  await eventually(() => completions.size >= 5);
  assert.deepEqual(acknowledged, []);
  assert.equal(concurrent, CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY);

  completions.get(queued[0].segmentId)?.();
  for (const next of queued.slice(3)) {
    await eventually(() => completions.has(next.segmentId));
    completions.get(next.segmentId)?.();
  }
  await eventually(() => acknowledged.length === queued.length);
  coordinator.stop();
  assert.equal(maxConcurrent, CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY);
  assert.deepEqual(acknowledged, queued.map((next) => next.segmentId));
  assert.equal((await spool.snapshot(queued[0].partition, now)).depth, 0);
});

test("前序 retryable failure 阻断越序提交且后序成功 receipt 不会重复上传", async () => {
  let now = 14_000;
  const spool = new MemoryCaptureUploadSpool();
  const first = item(1, { capturedAtMs: now });
  const second = item(2, { capturedAtMs: now });
  await spool.enqueue(first, now);
  await spool.enqueue(second, now);

  let firstAttempts = 0;
  let secondAttempts = 0;
  const acknowledged: string[] = [];
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (queued) => {
        if (queued.segmentId === first.segmentId) {
          firstAttempts += 1;
          if (firstAttempts === 1) throw new Error("retry head");
          return;
        }
        secondAttempts += 1;
      },
      retryDelay: () => 1_000,
      onAcknowledged: (queued) => acknowledged.push(queued.segmentId),
    },
    {
      now: () => now,
      maxConcurrentUploads: 2,
      schedule: (callback, delayMs) => {
        now += delayMs;
        queueMicrotask(callback);
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(first.partition);
  await eventually(() => acknowledged.length === 2);
  coordinator.stop();

  assert.equal(firstAttempts, 2);
  assert.equal(secondAttempts, 1);
  assert.deepEqual(acknowledged, [first.segmentId, second.segmentId]);
  assert.equal((await spool.snapshot(first.partition, now)).depth, 0);
});

test("队首退避期间继续上传后续 ordinal，恢复后仍按序 ack 且不重复", async () => {
  let now = 16_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = Array.from({ length: 12 }, (_, index) =>
    item(index + 1, { capturedAtMs: now }),
  );
  for (const next of queued) await spool.enqueue(next, now);

  const attempts = new Map<string, number>();
  const acknowledged: string[] = [];
  let wake: (() => void) | null = null;
  let wakeDelay = 0;
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (next) => {
        const count = (attempts.get(next.segmentId) ?? 0) + 1;
        attempts.set(next.segmentId, count);
        if (next.segmentId === queued[0].segmentId && count === 1) {
          throw new Error("retry head");
        }
      },
      retryDelay: () => 2_000,
      onAcknowledged: (next) => acknowledged.push(next.segmentId),
    },
    {
      now: () => now,
      maxConcurrentUploads: CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY,
      schedule: (callback, delayMs) => {
        wake = callback;
        wakeDelay = delayMs;
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(queued[0].partition);
  await eventually(() => attempts.size === queued.length);
  assert.deepEqual(acknowledged, []);
  assert.equal(attempts.get(queued[0].segmentId), 1);
  for (const next of queued.slice(1)) assert.equal(attempts.get(next.segmentId), 1);

  now += wakeDelay;
  if (!wake) throw new Error("retry wake was not scheduled");
  wake();
  await eventually(() => acknowledged.length === queued.length);
  coordinator.stop();

  assert.equal(attempts.get(queued[0].segmentId), 2);
  for (const next of queued.slice(1)) assert.equal(attempts.get(next.segmentId), 1);
  assert.deepEqual(acknowledged, queued.map((next) => next.segmentId));
  assert.equal((await spool.snapshot(queued[0].partition, now)).depth, 0);
});

test("reload 使用同一 durable spool 继续队首，且 origin 分区不会串传", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const originA = item(1, { origin: "https://a.example", capturedAtMs: 20_000 });
  const originB = item(2, { origin: "https://b.example", capturedAtMs: 20_000 });
  await spool.enqueue(originA, 20_000);
  await spool.enqueue(originB, 20_000);

  const uploaded: string[] = [];
  const firstRenderer = new CaptureUploadCoordinator(spool, {
    upload: async () => {
      throw new Error("renderer closed before acknowledgement");
    },
  }, { now: () => 20_000 });
  firstRenderer.start(originA.partition);
  firstRenderer.stop();

  const reloadedA = new CaptureUploadCoordinator(spool, {
    upload: async (queued) => uploaded.push(queued.origin),
  }, { now: () => 20_000 });
  reloadedA.start(originA.partition);
  await eventually(() => uploaded.length === 1);
  reloadedA.stop();
  assert.deepEqual(uploaded, ["https://a.example"]);
  assert.equal((await spool.snapshot(originB.partition, 20_000)).depth, 1);

  const switchedToB = new CaptureUploadCoordinator(spool, {
    upload: async (queued) => uploaded.push(queued.origin),
  }, { now: () => 20_000 });
  switchedToB.start(originB.partition);
  await eventually(() => uploaded.length === 2);
  switchedToB.stop();
  assert.deepEqual(uploaded, ["https://a.example", "https://b.example"]);
});

test("每个 partition 保留独立容量且全局容量仍有硬上限", async () => {
  const origins = [
    "https://a.example",
    "https://b.example",
    "https://c.example",
    "https://d.example",
  ];
  const seeded = origins.flatMap((origin, originIndex) =>
    Array.from({ length: CAPTURE_SPOOL_MAX_ITEMS }, (_, index) => {
      const sequence = originIndex * CAPTURE_SPOOL_MAX_ITEMS + index;
      return { ...item(sequence, { origin }), ordinal: sequence + 1 };
    }),
  );
  const spool = new MemoryCaptureUploadSpool(seeded);
  assert.equal(CAPTURE_SPOOL_GLOBAL_MAX_ITEMS, CAPTURE_SPOOL_MAX_ITEMS * 4);
  const rejected = await spool.enqueue(
    item(CAPTURE_SPOOL_GLOBAL_MAX_ITEMS, { origin: "https://e.example" }),
    1_000,
  );
  assert.deepEqual(
    {
      accepted: rejected.accepted,
      reason: rejected.accepted ? null : rejected.reason,
    },
    { accepted: false, reason: "global_count_limit" },
  );
});

test("durable spool 使用 8192/32768 与 1/4GiB 容量合同", () => {
  assert.equal(CAPTURE_SPOOL_MAX_ITEMS, 8_192);
  assert.equal(CAPTURE_SPOOL_GLOBAL_MAX_ITEMS, 32_768);
  assert.equal(CAPTURE_SPOOL_MAX_BYTES, 1_024 * 1024 * 1024);
  assert.equal(CAPTURE_SPOOL_GLOBAL_MAX_BYTES, 4_096 * 1024 * 1024);
  assert.equal(CAPTURE_SPOOL_TTL_MS, 24 * 60 * 60 * 1_000);
  assert.equal(CAPTURE_SPOOL_ACTIVE_RESERVED_ITEMS, CAPTURE_SPOOL_MAX_ITEMS);
  assert.equal(CAPTURE_SPOOL_ACTIVE_RESERVED_BYTES, CAPTURE_SPOOL_MAX_BYTES);
  assert.equal(CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY, 3);
  assert.equal(CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS, 4);
  assert.equal(CAPTURE_UPLOAD_RECOVERY_PARTITION_CONCURRENCY, 1);
  assert.deepEqual(
    [
      "count_limit",
      "byte_limit",
      "global_count_limit",
      "global_byte_limit",
    ].map((reason) =>
      isCaptureSpoolHardCapacityRejection(
        reason as Parameters<typeof isCaptureSpoolHardCapacityRejection>[0],
      ),
    ),
    [true, true, true, true],
  );
  assert.equal(isCaptureSpoolHardCapacityRejection("active_count_reserve"), false);
  assert.equal(isCaptureSpoolHardCapacityRejection("active_byte_reserve"), false);
});

test("恢复分区不能侵占实时分区保留的 8192 条容量", async () => {
  const active = item(10_000, { origin: "https://active.example" });
  const recoveryLimit =
    CAPTURE_SPOOL_GLOBAL_MAX_ITEMS - CAPTURE_SPOOL_ACTIVE_RESERVED_ITEMS;
  const spool = new MemoryCaptureUploadSpool(
    Array.from({ length: recoveryLimit }, (_, index) => ({
      ...item(index, {
        origin: `https://recovery-${Math.floor(index / CAPTURE_SPOOL_MAX_ITEMS)}.example`,
      }),
      ordinal: index + 1,
    })),
  );
  const recoveryRejected = await spool.enqueue(
    item(20_000, { origin: "https://recovery-overflow.example" }),
    1_000,
    { activePartition: active.partition },
  );
  assert.deepEqual(
    {
      accepted: recoveryRejected.accepted,
      reason: recoveryRejected.accepted ? null : recoveryRejected.reason,
    },
    { accepted: false, reason: "active_count_reserve" },
  );
  assert.equal(
    (
      await spool.enqueue(active, 1_000, {
        activePartition: active.partition,
      })
    ).accepted,
    true,
  );
});

test("恢复分区不能侵占实时分区保留的 1GiB 容量", async () => {
  const active = item(30_000, { origin: "https://active-bytes.example" });
  const recoverySeeds = Array.from({ length: 3 }, (_, index) => ({
    ...item(index, {
      origin: `https://recovery-bytes-${index}.example`,
    }),
    // Memory-spool seed models accounting without allocating multi-GiB Blobs.
    byteSize: CAPTURE_SPOOL_MAX_BYTES,
    ordinal: index + 1,
  }));
  const spool = new MemoryCaptureUploadSpool(recoverySeeds);
  const recoveryRejected = await spool.enqueue(
    item(30_001, { origin: "https://recovery-bytes-overflow.example" }),
    1_000,
    { activePartition: active.partition },
  );
  assert.deepEqual(
    {
      accepted: recoveryRejected.accepted,
      reason: recoveryRejected.accepted ? null : recoveryRejected.reason,
    },
    { accepted: false, reason: "active_byte_reserve" },
  );
  assert.equal(
    (
      await spool.enqueue(active, 1_000, {
        activePartition: active.partition,
      })
    ).accepted,
    true,
  );
});

test("recovery 单槽占满时新 active 立即获得三个独立槽且同 partition 重申不重启", async () => {
  const now = 80_000;
  const spool = new MemoryCaptureUploadSpool();
  const partitions: string[] = [];
  for (let index = 0; index < 6; index += 1) {
    const queued = item(index, {
      origin: `https://partition-${index}.example`,
      capturedAtMs: now,
    });
    partitions.push(queued.partition);
    await spool.enqueue(queued, now);
  }
  for (let index = 0; index < 7; index += 1) {
    await spool.enqueue(
      item(100 + index, {
        origin: "https://partition-0.example",
        capturedAtMs: now,
      }),
      now,
    );
  }
  for (let index = 0; index < 7; index += 1) {
    await spool.enqueue(
      item(200 + index, {
        origin: "https://partition-1.example",
        capturedAtMs: now,
      }),
      now,
    );
  }

  let concurrent = 0;
  let maxConcurrent = 0;
  const attempts: string[] = [];
  let latestActivity: CaptureUploadPoolActivity | null = null;
  const pool = new CaptureUploadPool(
    spool,
    {
      upload: (queued, signal) =>
        new Promise<void>((_resolve, reject) => {
          concurrent += 1;
          maxConcurrent = Math.max(maxConcurrent, concurrent);
          attempts.push(queued.partition);
          signal.addEventListener(
            "abort",
            () => {
              concurrent -= 1;
              reject(signal.reason);
            },
            { once: true },
          );
        }),
      onPoolActivity: (snapshot) => {
        latestActivity = snapshot;
        assert.ok(snapshot.activeInFlightCurrent <= 3);
        assert.ok(snapshot.recoveryInFlightCurrent <= 1);
        assert.ok(snapshot.globalInFlightCurrent <= 4);
      },
      isPartitionEligible: ({ partition }) =>
        partition === partitions[0] || partition === partitions[1],
    },
    { now: () => now },
  );
  pool.start();
  await eventually(() => latestActivity?.recoveryInFlightCurrent === 1);
  assert.equal(latestActivity?.activeInFlightCurrent, 0);
  const beforeClaimAttempts = attempts.length;

  assert.equal(pool.setActivePartition(partitions[0]), true);
  await eventually(
    () =>
      latestActivity?.activeInFlightCurrent === 3 &&
      latestActivity?.recoveryInFlightCurrent === 1,
  );
  const claimedAttempts = attempts.slice(beforeClaimAttempts);
  assert.equal(
    claimedAttempts.filter((partition) => partition === partitions[0]).length,
    3,
  );
  assert.equal(latestActivity?.activeInFlightMax, 3);
  assert.equal(latestActivity?.recoveryInFlightMax, 1);
  assert.equal(maxConcurrent, 4);
  assert.equal(pool.setActivePartition(partitions[0]), false);
  assert.equal(latestActivity?.activeInFlightCurrent, 3);

  const beforeSwitchAttempts = attempts.length;
  pool.setActivePartition(partitions[5]);
  pool.kick();
  await eventually(() =>
    attempts.slice(beforeSwitchAttempts).includes(partitions[5]),
  );
  assert.equal(maxConcurrent, 4);
  pool.dispose();
});

test("四分钟持续产片下 active queue 不增长且 recovery backlog 不会饿死实时上传", async () => {
  const startMs = 100_000;
  const runtimeMs = 4 * 60 * 1_000;
  const arrivalIntervalMs = 789;
  const uploadLatencyMs = 1_150;
  const scheduler = new VirtualScheduler(startMs);
  const spool = new MemoryCaptureUploadSpool();
  const activeTemplate = item(50_000, {
    origin: "https://active-pressure.example",
    capturedAtMs: startMs,
  });
  const recoveryTemplates = Array.from({ length: 4 }, (_, index) =>
    item(60_000 + index, {
      origin: `https://recovery-pressure-${index}.example`,
      capturedAtMs: startMs,
    }),
  );
  const recoveryDepth = 24;
  for (const [partitionIndex, template] of recoveryTemplates.entries()) {
    for (let index = 0; index < recoveryDepth; index += 1) {
      await spool.enqueue(
        item(80_000 + partitionIndex * recoveryDepth + index, {
          origin: template.origin,
          capturedAtMs: startMs,
        }),
        startMs,
      );
    }
  }

  let totalInFlight = 0;
  let activeInFlight = 0;
  let maxTotalInFlight = 0;
  let maxActiveInFlight = 0;
  let activeAcknowledged = 0;
  let recoveryAcknowledged = 0;
  const pool = new CaptureUploadPool(
    spool,
    {
      upload: (queued, signal) =>
        new Promise<void>((resolve, reject) => {
          let settled = false;
          totalInFlight += 1;
          if (queued.partition === activeTemplate.partition) activeInFlight += 1;
          maxTotalInFlight = Math.max(maxTotalInFlight, totalInFlight);
          maxActiveInFlight = Math.max(maxActiveInFlight, activeInFlight);
          const settle = (error?: unknown) => {
            if (settled) return;
            settled = true;
            totalInFlight -= 1;
            if (queued.partition === activeTemplate.partition) activeInFlight -= 1;
            if (error === undefined) resolve();
            else reject(error);
          };
          const handle = scheduler.schedule(() => settle(), uploadLatencyMs);
          signal.addEventListener(
            "abort",
            () => {
              scheduler.cancel(handle);
              settle(signal.reason);
            },
            { once: true },
          );
        }),
      onAcknowledged: (queued) => {
        if (queued.partition === activeTemplate.partition) activeAcknowledged += 1;
        else recoveryAcknowledged += 1;
      },
    },
    {
      now: scheduler.now,
      schedule: scheduler.schedule,
      cancelSchedule: scheduler.cancel,
    },
  );
  pool.setActivePartition(activeTemplate.partition);
  pool.start();

  let arrivals = 0;
  let dropped = 0;
  let maxActiveDepth = 0;
  const depthSamples: number[] = [];
  for (let offsetMs = 0; offsetMs < runtimeMs; offsetMs += arrivalIntervalMs) {
    scheduler.schedule(() => {
      const queued = item(90_000 + arrivals, {
        origin: activeTemplate.origin,
        capturedAtMs: scheduler.now(),
      });
      arrivals += 1;
      void spool
        .enqueue(queued, scheduler.now(), {
          activePartition: activeTemplate.partition,
        })
        .then((result) => {
          if (!result.accepted) dropped += 1;
          else maxActiveDepth = Math.max(maxActiveDepth, result.snapshot.depth);
          pool.kick();
        });
    }, offsetMs);
  }
  for (let offsetMs = 30_000; offsetMs <= runtimeMs; offsetMs += 30_000) {
    scheduler.schedule(() => {
      void spool
        .snapshot(activeTemplate.partition, scheduler.now())
        .then((snapshot) => depthSamples.push(snapshot.depth));
    }, offsetMs);
  }

  await scheduler.runUntil(startMs + runtimeMs);
  await scheduler.runUntil(startMs + runtimeMs + 10_000);
  const finalActive = await spool.snapshot(
    activeTemplate.partition,
    scheduler.now(),
  );
  pool.dispose();

  assert.equal(dropped, 0);
  assert.equal(activeAcknowledged, arrivals);
  assert.equal(recoveryAcknowledged, recoveryDepth * recoveryTemplates.length);
  assert.ok(maxActiveInFlight >= 1 && maxActiveInFlight <= 3);
  assert.ok(maxTotalInFlight >= 2 && maxTotalInFlight <= 4);
  assert.equal(finalActive.depth, 0);
  assert.ok(maxActiveDepth < CAPTURE_SPOOL_MAX_ITEMS, `active depth reached ${maxActiveDepth}`);
  assert.ok(
    depthSamples.every((depth) => depth < CAPTURE_SPOOL_MAX_ITEMS),
    `active depth samples were ${depthSamples.join(",")}`,
  );
});

test("历史分区退避会让出恢复槽位，不会阻塞更晚的可发送 partition", async () => {
  const now = 90_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = Array.from({ length: 5 }, (_, index) =>
    item(index, {
      origin: `https://fair-${index}.example`,
      capturedAtMs: now,
    }),
  );
  for (const next of queued) await spool.enqueue(next, now);
  const attempts: string[] = [];
  const pool = new CaptureUploadPool(
    spool,
    {
      upload: async (next) => {
        attempts.push(next.partition);
        if (next.partition !== queued[0].partition) throw new Error("retry");
      },
    },
    { now: () => now },
  );
  pool.start();
  await eventually(() => attempts.includes(queued[4].partition));
  pool.dispose();
  assert.equal(attempts.includes(queued[0].partition), true);
  assert.equal(attempts.includes(queued[4].partition), true);
});

test("formal partition idle waiter 不被其它 recovery partition 的 in-flight 阻塞", async () => {
  const now = 91_000;
  const spool = new MemoryCaptureUploadSpool();
  const active = item(1, { origin: "https://formal.example", capturedAtMs: now });
  const recovery = item(2, { origin: "https://recovery.example", capturedAtMs: now });
  await spool.enqueue(active, now);
  await spool.enqueue(recovery, now);

  let recoveryStarted = false;
  const pool = new CaptureUploadPool(
    spool,
    {
      isPartitionEligible: () => true,
      upload: async (queued, signal) => {
        if (queued.partition === active.partition) return durableReceipt(queued);
        recoveryStarted = true;
        await new Promise<never>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
        throw new Error("unreachable");
      },
    },
    { now: () => now },
  );
  pool.setActivePartition(active.partition);
  pool.start();
  await eventually(() => recoveryStarted);
  await pool.awaitPartitionIdle(active.partition, 100);
  assert.equal((await spool.snapshot(active.partition, now)).depth, 0);
  pool.dispose();
});

test("恢复池只消费通过 origin/principal/device fence 的 partition", async () => {
  const now = 95_000;
  const spool = new MemoryCaptureUploadSpool();
  const active = item(1, {
    origin: "https://eligible.example",
    principalScope: "scope-current",
    deviceId: "device-current",
    capturedAtMs: now,
  });
  const foreignOrigin = item(2, {
    origin: "https://foreign.example",
    principalScope: "scope-current",
    deviceId: "device-current",
    capturedAtMs: now,
  });
  const foreignPrincipal = item(3, {
    origin: "https://eligible.example",
    principalScope: "scope-foreign",
    deviceId: "device-current",
    capturedAtMs: now,
  });
  await spool.enqueue(active, now);
  await spool.enqueue(foreignOrigin, now);
  await spool.enqueue(foreignPrincipal, now);
  const attempts: string[] = [];
  let resolveActive = () => undefined;
  const activeDone = new Promise<void>((resolve) => {
    resolveActive = resolve;
  });
  const pool = new CaptureUploadPool(spool, {
    isPartitionEligible: (summary) =>
      summary.origin === active.origin &&
      summary.principalScope === active.principalScope,
    upload: async (next) => {
      attempts.push(next.partition);
    },
    onAcknowledged: (next) => {
      if (next.partition === active.partition) resolveActive();
    },
  }, { now: () => now });
  pool.setActivePartition(active.partition);
  pool.start();
  await activeDone;
  await new Promise((resolve) => setImmediate(resolve));
  pool.dispose();
  assert.deepEqual(attempts, [active.partition]);
  assert.equal((await spool.snapshot(foreignOrigin.partition, now)).depth, 1);
  assert.equal((await spool.snapshot(foreignPrincipal.partition, now)).depth, 1);
});

test("同 origin/principal/device 的旧 v2 session 可 recovery drain，且 active owner 不被夺取", async () => {
  const now = 96_000;
  const active = item(1, {
    origin: "https://eligible.example",
    principalScope: "scope-current",
    deviceId: "device-current",
    captureSessionId: "session-current",
    capturedAtMs: now,
  });
  const oldSession = item(2, {
    origin: active.origin,
    principalScope: active.principalScope,
    deviceId: active.scope.deviceId,
    captureSessionId: "session-old",
    capturedAtMs: now - 10 * 60 * 1_000,
  });
  const foreignOrigin = item(3, {
    origin: "https://foreign.example",
    principalScope: active.principalScope,
    deviceId: active.scope.deviceId,
    captureSessionId: "session-foreign-origin",
    capturedAtMs: now,
  });
  const foreignPrincipal = item(4, {
    origin: active.origin,
    principalScope: "scope-foreign",
    deviceId: active.scope.deviceId,
    captureSessionId: "session-foreign-principal",
    capturedAtMs: now,
  });
  const foreignDevice = item(5, {
    origin: active.origin,
    principalScope: active.principalScope,
    deviceId: "device-foreign",
    captureSessionId: "session-foreign-device",
    capturedAtMs: now,
  });
  const spool = new MemoryCaptureUploadSpool();
  for (const queued of [active, oldSession, foreignOrigin, foreignPrincipal, foreignDevice]) {
    await spool.enqueue(queued, now);
  }

  const attempts: string[] = [];
  const acknowledged: string[] = [];
  const pool = new CaptureUploadPool<TestCaptureReceipt>(spool, {
    isPartitionEligible: (summary) =>
      summary.origin === active.origin &&
      summary.principalScope === active.principalScope &&
      summary.deviceId === active.scope.deviceId &&
      isCaptureSpoolPartitionCompatible(
        summary.partition,
        summary.origin,
        summary.principalScope,
        summary.deviceId,
        summary.captureSessionId,
      ),
    upload: async (queued) => {
      attempts.push(queued.partition);
      return durableReceipt(queued);
    },
    isAcknowledgementValid: (queued, result) => isDurableReceipt(queued, result),
    onAcknowledged: (queued) => acknowledged.push(queued.partition),
  }, { now: () => now });

  pool.setActivePartition(active.partition);
  pool.start();
  await eventually(() => acknowledged.includes(oldSession.partition));
  assert.equal(pool.currentActivePartition(), active.partition);
  pool.dispose();

  assert.equal(pool.currentActivePartition(), null);
  assert.equal(attempts.includes(active.partition), true);
  assert.equal(attempts.includes(oldSession.partition), true);
  assert.equal((await spool.snapshot(oldSession.partition, now)).depth, 0);
  assert.equal((await spool.snapshot(foreignOrigin.partition, now)).depth, 1);
  assert.equal((await spool.snapshot(foreignPrincipal.partition, now)).depth, 1);
  assert.equal((await spool.snapshot(foreignDevice.partition, now)).depth, 1);
});

test("同一 operation 跨 active/recovery partition 只发一次真实 HTTP", async () => {
  const now = 97_000;
  const active = item(1, {
    capturedAtMs: now,
    captureSessionId: "session-current",
  });
  const recovery = {
    ...active,
    partition: captureSpoolPartition(
      active.origin,
      active.principalScope,
      active.scope.deviceId,
      active.scope.captureSessionId ?? "",
      "formal",
    ),
    segmentId: "segment-duplicate-recovery",
  };
  const spool = new MemoryCaptureUploadSpool();
  await spool.enqueue(active, now);
  await spool.enqueue(recovery, now);

  let releaseUpload = () => undefined;
  let attempts = 0;
  let acknowledgements = 0;
  const pool = new CaptureUploadPool<TestCaptureReceipt>(spool, {
    upload: (queued) => {
      attempts += 1;
      return new Promise<TestCaptureReceipt>((resolve) => {
        releaseUpload = () => resolve(durableReceipt(queued));
      });
    },
    isAcknowledgementValid: (queued, result) => isDurableReceipt(queued, result),
    onAcknowledged: () => {
      acknowledgements += 1;
    },
  }, { now: () => now });

  pool.setActivePartition(active.partition);
  pool.start();
  await eventually(() => attempts === 1);
  assert.equal(acknowledgements, 0);
  releaseUpload();
  await eventually(() => acknowledgements === 2);
  pool.dispose();

  assert.equal(attempts, 1);
  assert.equal((await spool.snapshot(active.partition, now)).depth, 0);
  assert.equal((await spool.snapshot(recovery.partition, now)).depth, 0);
});

test("空队列 peek 期间的新 kick 不会丢失唤醒", async () => {
  const inner = new MemoryCaptureUploadSpool();
  let releasePeek = () => undefined;
  let markPeekStarted = () => undefined;
  const peekStarted = new Promise<void>((resolve) => {
    markPeekStarted = resolve;
  });
  const peekRelease = new Promise<void>((resolve) => {
    releasePeek = resolve;
  });
  let firstPeek = true;
  const spool: CaptureUploadSpool = {
    enqueue: (queued, now, options) => inner.enqueue(queued, now, options),
    acknowledge: (ordinal) => inner.acknowledge(ordinal),
    markRetry: (ordinal, retryCount, nextAttemptAtMs) =>
      inner.markRetry(ordinal, retryCount, nextAttemptAtMs),
    snapshot: (partition, now) => inner.snapshot(partition, now),
    listPartitions: (now) => inner.listPartitions(now),
    peek: async (partition, now) => {
      const frozen = await inner.peek(partition, now);
      if (firstPeek) {
        firstPeek = false;
        markPeekStarted();
        await peekRelease;
      }
      return frozen;
    },
  };
  const queued = item(1, { capturedAtMs: 30_000 });
  let uploads = 0;
  const coordinator = new CaptureUploadCoordinator(spool, {
    upload: async () => {
      uploads += 1;
    },
  }, { now: () => 30_000 });
  coordinator.start(queued.partition);
  await peekStarted;
  await spool.enqueue(queued, 30_000);
  coordinator.kick();
  releasePeek();
  await eventually(() => uploads === 1);
  coordinator.stop();
});

test("origin 在 delayed peek 中切换时旧 item 不会上传", async () => {
  const inner = new MemoryCaptureUploadSpool();
  const originA = item(1, { origin: "https://a.example", capturedAtMs: 40_000 });
  const originB = item(2, { origin: "https://b.example", capturedAtMs: 40_000 });
  await inner.enqueue(originA, 40_000);
  await inner.enqueue(originB, 40_000);
  let releasePeek = () => undefined;
  let markPeekStarted = () => undefined;
  const peekStarted = new Promise<void>((resolve) => {
    markPeekStarted = resolve;
  });
  const peekRelease = new Promise<void>((resolve) => {
    releasePeek = resolve;
  });
  let delayed = true;
  const spool: CaptureUploadSpool = {
    enqueue: (queued, now, options) => inner.enqueue(queued, now, options),
    acknowledge: (ordinal) => inner.acknowledge(ordinal),
    markRetry: (ordinal, retryCount, nextAttemptAtMs) =>
      inner.markRetry(ordinal, retryCount, nextAttemptAtMs),
    snapshot: (partition, now) => inner.snapshot(partition, now),
    listPartitions: (now) => inner.listPartitions(now),
    peek: async (partition, now) => {
      const frozen = await inner.peek(partition, now);
      if (delayed) {
        delayed = false;
        markPeekStarted();
        await peekRelease;
      }
      return frozen;
    },
  };
  const uploaded: string[] = [];
  const coordinator = new CaptureUploadCoordinator(spool, {
    upload: async (queued) => uploaded.push(queued.origin),
  }, { now: () => 40_000 });
  coordinator.start(originA.partition);
  await peekStarted;
  coordinator.start(originB.partition);
  releasePeek();
  await eventually(() => uploaded.length === 1);
  coordinator.stop();
  assert.deepEqual(uploaded, ["https://b.example"]);
  assert.equal((await inner.snapshot(originA.partition, 40_000)).depth, 1);
});

test("completed idempotent replay 的 status=accepted durable receipt 可 ack", async () => {
  const inner = new MemoryCaptureUploadSpool();
  const queued = item(1, { capturedAtMs: 50_000 });
  await inner.enqueue(queued, 50_000);
  let acknowledgeCalls = 0;
  const spool: CaptureUploadSpool = {
    enqueue: (next, now, options) => inner.enqueue(next, now, options),
    peek: (partition, now) => inner.peek(partition, now),
    snapshot: (partition, now) => inner.snapshot(partition, now),
    markRetry: (ordinal, retryCount, nextAttemptAtMs) =>
      inner.markRetry(ordinal, retryCount, nextAttemptAtMs),
    listPartitions: (now) => inner.listPartitions(now),
    acknowledge: async (ordinal) => {
      acknowledgeCalls += 1;
      if (acknowledgeCalls === 1) throw new Error("temporary ack failure");
      await inner.acknowledge(ordinal);
    },
  };
  let uploads = 0;
  const receipt = durableReceipt(queued);
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async () => {
        uploads += 1;
        return receipt;
      },
      isAcknowledgementValid: (item, result) => isDurableReceipt(item, result),
    },
    {
      now: () => 50_000,
      schedule: (callback) => {
        queueMicrotask(callback);
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(queued.partition);
  await eventually(() => acknowledgeCalls === 2);
  coordinator.stop();
  assert.equal(uploads, 1);
  assert.equal((await inner.snapshot(queued.partition, 50_000)).depth, 0);
});

test("只有首次 durable receipt 才确认并删除队首", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(1, { capturedAtMs: 55_000 });
  await spool.enqueue(queued, 55_000);
  let acknowledged = 0;
  const coordinator = new CaptureUploadCoordinator<TestCaptureReceipt>(
    spool,
    {
      upload: async () => durableReceipt(queued),
      isAcknowledgementValid: (next, result) => isDurableReceipt(next, result),
      onAcknowledged: () => {
        acknowledged += 1;
      },
    },
    { now: () => 55_000 },
  );
  coordinator.start(queued.partition);
  await eventually(() => acknowledged === 1);
  coordinator.stop();
  assert.equal((await spool.snapshot(queued.partition, 55_000)).depth, 0);
});

test("missing、pending、rejected 或 scope mismatch receipt 保留队首并 retry", async () => {
  const invalidReceipts: Array<TestCaptureReceipt | undefined> = [
    undefined,
    {
      ...durableReceipt(item(2)),
      admission: {
        status: "rejected",
        durable: true,
        receipt_id: null,
      },
    },
    {
      ...durableReceipt(item(3)),
      admission: {
        status: "accepted",
        durable: false,
        receipt_id: "capture-not-durable",
      },
    },
    {
      ...durableReceipt(item(4)),
      admission: {
        status: "pending",
        durable: false,
        receipt_id: null,
      },
    },
    {
      ...durableReceipt(item(5)),
      admission: {
        status: "accepted",
        durable: true,
        receipt_id: null,
      },
    },
    {
      ...durableReceipt(item(5)),
      device_id: "device-foreign",
    },
  ];

  for (const [index, invalid] of invalidReceipts.entries()) {
    const now = 56_000 + index;
    const spool = new MemoryCaptureUploadSpool();
    const queued = item(index + 10, { capturedAtMs: now });
    await spool.enqueue(queued, now);
    let attempts = 0;
    let acknowledged = 0;
    const coordinator = new CaptureUploadCoordinator<TestCaptureReceipt>(
      spool,
      {
        upload: async () => {
          attempts += 1;
          return invalid as TestCaptureReceipt;
        },
        isAcknowledgementValid: (next, result) => isDurableReceipt(next, result),
        onAcknowledged: () => {
          acknowledged += 1;
        },
      },
      { now: () => now },
    );
    coordinator.start(queued.partition);
    await eventually(() => attempts === 1);
    coordinator.stop();
    assert.equal(acknowledged, 0);
    assert.equal((await spool.snapshot(queued.partition, now)).depth, 1);
  }
});

test("旧 v2 session 的 response scope mismatch 不 ack", async () => {
  const now = 58_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(60, {
    captureSessionId: "session-old",
    capturedAtMs: now,
  });
  await spool.enqueue(queued, now);
  let attempts = 0;
  let acknowledged = 0;
  const coordinator = new CaptureUploadCoordinator<TestCaptureReceipt>(spool, {
    upload: async () => {
      attempts += 1;
      return {
        ...durableReceipt(queued),
        capture_session_id: "session-current",
      };
    },
    isAcknowledgementValid: (next, result) => isDurableReceipt(next, result),
    onAcknowledged: () => {
      acknowledged += 1;
    },
  }, { now: () => now });

  coordinator.start(queued.partition);
  await eventually(() => attempts === 1);
  coordinator.stop();

  assert.equal(acknowledged, 0);
  assert.equal((await spool.snapshot(queued.partition, now)).depth, 1);
});

test("retryable admission 保留同一队首并尊重服务端退避", async () => {
  let now = 60_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(1, { capturedAtMs: now });
  await spool.enqueue(queued, now);
  const attempts: string[] = [];
  const delays: number[] = [];
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (next) => {
        attempts.push(next.idempotencyKey);
        if (attempts.length === 1) throw new Error("retryable admission");
      },
      retryDelay: () => 7_000,
    },
    {
      now: () => now,
      schedule: (callback, delayMs) => {
        delays.push(delayMs);
        now += delayMs;
        queueMicrotask(callback);
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(queued.partition);
  await eventually(() => attempts.length === 2);
  coordinator.stop();
  assert.deepEqual(attempts, ["capture-1", "capture-1"]);
  assert.deepEqual(delays, [7_000]);
});

test("429 与 503 均保留 durable 队首并在退避后使用同一幂等键恢复", async () => {
  for (const status of [429, 503]) {
    let now = 65_000;
    const spool = new MemoryCaptureUploadSpool();
    const queued = item(status, { capturedAtMs: now });
    await spool.enqueue(queued, now);
    const attempts: string[] = [];
    const delays: number[] = [];
    const coordinator = new CaptureUploadCoordinator(
      spool,
      {
        upload: async (next) => {
          attempts.push(next.idempotencyKey);
          if (attempts.length === 1) {
            throw { status, retryAfterMs: 2_500 };
          }
        },
        shouldRetry: (error) => {
          const candidate = error as { status?: number };
          return candidate.status === 429 || (candidate.status ?? 0) >= 500;
        },
        retryDelay: (error) =>
          (error as { retryAfterMs?: number }).retryAfterMs ?? null,
      },
      {
        now: () => now,
        schedule: (callback, delayMs) => {
          delays.push(delayMs);
          now += delayMs;
          queueMicrotask(callback);
          return 1 as ReturnType<typeof setTimeout>;
        },
        cancelSchedule: () => undefined,
      },
    );
    coordinator.start(queued.partition);
    await eventually(() => attempts.length === 2);
    coordinator.stop();
    assert.deepEqual(attempts, [queued.idempotencyKey, queued.idempotencyKey]);
    assert.deepEqual(delays, [2_500]);
    assert.equal((await spool.snapshot(queued.partition, now)).depth, 0);
  }
});

test("无 Retry-After 的 503 使用指数退避并自动重放", async () => {
  let now = 68_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(503, { capturedAtMs: now });
  await spool.enqueue(queued, now);
  const attempts: string[] = [];
  const delays: number[] = [];
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (next) => {
        attempts.push(next.idempotencyKey);
        if (attempts.length < 3) throw { status: 503 };
      },
      shouldRetry: (error) => (error as { status?: number }).status === 503,
    },
    {
      now: () => now,
      schedule: (callback, delayMs) => {
        delays.push(delayMs);
        now += delayMs;
        queueMicrotask(callback);
        return 1 as ReturnType<typeof setTimeout>;
      },
      cancelSchedule: () => undefined,
    },
  );
  coordinator.start(queued.partition);
  await eventually(() => attempts.length === 3);
  coordinator.stop();
  assert.deepEqual(
    attempts,
    [queued.idempotencyKey, queued.idempotencyKey, queued.idempotencyKey],
  );
  assert.deepEqual(delays, [1_000, 2_000]);
  assert.equal((await spool.snapshot(queued.partition, now)).depth, 0);
});

test("ASR 无可用 provider 时 5 秒内不得把同一队列变成 1 秒重试风暴", async () => {
  const startMs = 69_000;
  const scheduler = new VirtualScheduler(startMs);
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(690, { capturedAtMs: startMs });
  await spool.enqueue(queued, startMs);
  let attempts = 0;
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async () => {
        attempts += 1;
        throw {
          status: 503,
          errorClass: "asr_no_eligible_provider",
          retryAfterMs: null,
        };
      },
      retryDelay: captureUploadRetryDelay,
    },
    {
      now: scheduler.now,
      schedule: scheduler.schedule,
      cancelSchedule: scheduler.cancel,
    },
  );
  coordinator.start(queued.partition);
  await eventually(() => attempts === 1);

  await scheduler.runUntil(startMs + 5_000);
  coordinator.stop();

  assert.equal(attempts, 1);
  assert.equal((await spool.snapshot(queued.partition, scheduler.now())).depth, 1);
});

test("明确永久错误不删除队首，也不伪造确认", async () => {
  const spool = new MemoryCaptureUploadSpool();
  const first = item(1, { capturedAtMs: 70_000 });
  await spool.enqueue(first, 70_000);
  const attempts: string[] = [];
  const discarded: string[] = [];
  const coordinator = new CaptureUploadCoordinator(
    spool,
    {
      upload: async (next) => {
        attempts.push(next.segmentId);
        throw new TypeError("invalid chunk");
      },
      shouldRetry: (error) => !(error instanceof TypeError),
      onDiscarded: (next) => discarded.push(next.segmentId),
    },
    { now: () => 70_000 },
  );
  coordinator.start(first.partition);
  await eventually(() => attempts.length === 1);
  coordinator.stop();
  assert.deepEqual(attempts, ["segment-1"]);
  assert.deepEqual(discarded, []);
  assert.equal((await spool.snapshot(first.partition, 70_000)).depth, 1);
});

test("pool 也必须只在 durable receipt 通过时推进 ack cursor", async () => {
  let now = 72_000;
  const spool = new MemoryCaptureUploadSpool();
  const queued = item(1, { capturedAtMs: now });
  await spool.enqueue(queued, now);
  const pendingReceipt: TestCaptureReceipt = {
    ...durableReceipt(queued),
    admission: {
      status: "pending",
      durable: false,
      receipt_id: null,
    },
  };
  const attempts: string[] = [];
  const pool = new CaptureUploadPool<TestCaptureReceipt>(spool, {
    upload: async (next) => {
      attempts.push(next.idempotencyKey);
      if (attempts.length === 1) return pendingReceipt;
      return durableReceipt(next);
    },
    isAcknowledgementValid: (next, result) => isDurableReceipt(next, result),
    retryDelay: () => 1_000,
  }, {
    now: () => now,
    schedule: (callback, delayMs) => {
      now += delayMs;
      queueMicrotask(callback);
      return 1 as ReturnType<typeof setTimeout>;
    },
    cancelSchedule: () => undefined,
  });
  pool.setActivePartition(queued.partition);
  pool.start();
  await eventually(() => attempts.length === 2);
  pool.dispose();
  assert.deepEqual(attempts, [queued.idempotencyKey, queued.idempotencyKey]);
  assert.equal((await spool.snapshot(queued.partition, now)).depth, 0);
});

test("formal partition idle waiter observes queue and in-flight reaching zero", async () => {
  const now = Date.now();
  const queued = item(7, { meetingId: "meeting-formal", capturedAtMs: now });
  const spool = new MemoryCaptureUploadSpool();
  await spool.enqueue(queued, now);
  let releaseUpload!: () => void;
  const pool = new CaptureUploadPool<TestCaptureReceipt>(spool, {
    upload: () => new Promise<TestCaptureReceipt>((resolve) => {
      releaseUpload = () => resolve(durableReceipt(queued));
    }),
    isAcknowledgementValid: isDurableReceipt,
  });
  pool.setActivePartition(queued.partition);
  pool.start();
  await eventually(() => typeof releaseUpload === "function");
  const idle = pool.awaitPartitionIdle(queued.partition, 500);
  releaseUpload();
  await idle;
  assert.equal((await spool.snapshot(queued.partition)).depth, 0);
  pool.dispose();
});

test("formal partition idle waiter is bounded when an upload never settles", async () => {
  const now = Date.now();
  const queued = item(8, { meetingId: "meeting-timeout", capturedAtMs: now });
  const spool = new MemoryCaptureUploadSpool();
  await spool.enqueue(queued, now);
  const pool = new CaptureUploadPool(spool, {
    upload: () => new Promise(() => undefined),
  });
  pool.setActivePartition(queued.partition);
  pool.start();
  await assert.rejects(
    pool.awaitPartitionIdle(queued.partition, 10),
    /timed out/i,
  );
  pool.dispose();
});
