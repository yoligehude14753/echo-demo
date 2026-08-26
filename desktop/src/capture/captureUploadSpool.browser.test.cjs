"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const test = require("node:test");

const { chromium } = require("@playwright/test");
const { build } = require("esbuild");

async function browserBundle() {
  const result = await build({
    stdin: {
      contents: [
        'export * from "./src/capture/captureUploadSpool.ts";',
        'export * from "./src/capture/captureUploadCoordinator.ts";',
      ].join("\n"),
      resolveDir: path.resolve(__dirname, "../.."),
      sourcefile: "capture-upload-browser-entry.ts",
    },
    bundle: true,
    format: "iife",
    globalName: "CaptureUploadTest",
    platform: "browser",
    write: false,
  });
  return result.outputFiles[0].text;
}

async function withBrowser(run) {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end("<!doctype html><meta charset=utf-8>");
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${address.port}/`);
    await page.addScriptTag({ content: await browserBundle() });
    await run(page);
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

test("真实 IndexedDB 在新实例恢复顺序、稳定 key 与全局原子容量", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-spool-test-${crypto.randomUUID()}`;
      const now = Date.now();
      const makeItem = (sequence, origin) => {
        const principalScope = "scope-test";
        const deviceId = "device-test";
        const captureSessionId = "capture-session-test";
        return {
          partition: api.captureSpoolPartition(
            origin,
            principalScope,
            deviceId,
            captureSessionId,
          ),
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId,
            captureSessionId,
            source: "desktop",
          },
          segmentId: `segment-${sequence}`,
          idempotencyKey: `capture-segment-${sequence}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        };
      };
      const origins = [
        "https://a.example",
        "https://b.example",
        "https://c.example",
        "https://d.example",
      ];
      const originA = origins[0];
      const first = new api.IndexedDbCaptureUploadSpool(databaseName);
      await first.enqueue(makeItem(1, originA), now);
      await first.enqueue(makeItem(2, originA), now);

      const reloaded = new api.IndexedDbCaptureUploadSpool(databaseName);
      const partitionA = makeItem(1, originA).partition;
      const restored = await reloaded.peek(partitionA, now);
      const stableKey = restored.item?.idempotencyKey === "capture-segment-1";
      const stableSize = restored.item?.byteSize === 4;

      const writers = [first, reloaded];
      const attempts = [];
      for (let index = 2; index < 102; index += 1) {
        attempts.push(
          writers[index % writers.length].enqueue(
            makeItem(index + 1, origins[index % origins.length]),
            now,
          ),
        );
      }
      const outcomes = await Promise.all(attempts);
      const snapshot = await reloaded.snapshot(partitionA, now);
      return {
        restoredDepth: restored.snapshot.depth,
        stableKey,
        stableSize,
        accepted: outcomes.filter((outcome) => outcome.accepted).length,
        rejected: outcomes.filter((outcome) => !outcome.accepted).length,
        globalDepth: snapshot.globalDepth,
        globalCapacity: api.CAPTURE_SPOOL_GLOBAL_MAX_ITEMS,
      };
    });
    assert.deepEqual(result, {
      restoredDepth: 2,
      stableKey: true,
      stableSize: true,
        accepted: 100,
        rejected: 0,
        globalDepth: 102,
        globalCapacity: 32_768,
    });
  });
});

test("legacy IndexedDB 在 generation cutover 后原子清空，后续只保留新 generation", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-legacy-test-${crypto.randomUUID()}`;
      const origin = "https://legacy.example";
      const principalScope = "scope-legacy";
      const deviceId = "device-legacy";
      const partition = api.captureSpoolLegacyPartition(
        origin,
        principalScope,
        deviceId,
      );
      const now = Date.now();
      const legacy = await new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName, 1);
        request.onupgradeneeded = () => {
          const chunks = request.result.createObjectStore("chunks", {
            keyPath: "ordinal",
            autoIncrement: true,
          });
          chunks.createIndex("partition", "partition", { unique: false });
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      const transaction = legacy.transaction("chunks", "readwrite");
      const chunks = transaction.objectStore("chunks");
      for (let sequence = 1; sequence <= 3; sequence += 1) {
        chunks.add({
          partition,
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now + sequence,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId,
            source: "desktop",
          },
          segmentId: `segment-${sequence}`,
          idempotencyKey: `capture-segment-${sequence}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        });
      }
      await new Promise((resolve, reject) => {
        transaction.oncomplete = resolve;
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
      legacy.close();

      const spool = new api.IndexedDbCaptureUploadSpool(databaseName);
      const restored = await spool.peekBatch(partition, 3, now);
      const inventory = await spool.listPartitions(now);
      const afterCutover = await spool.snapshot(partition, now);
      return {
        restoredDepth: restored.snapshot.depth,
        globalDepth: inventory.globalDepth,
        partitionCount: inventory.partitions.length,
        ordinals: restored.items.map((item) => item.ordinal),
        afterCutoverDepth: afterCutover.depth,
      };
    });
    assert.deepEqual(result, {
      restoredDepth: 0,
      globalDepth: 0,
      partitionCount: 0,
      ordinals: [],
      afterCutoverDepth: 0,
    });
  });
});

test("Web Lock 让两个 coordinator 对同一 durable partition 单消费", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-lock-test-${crypto.randomUUID()}`;
      const origin = "https://capture.example";
      const principalScope = "scope-test";
      const deviceId = "device-test";
      const partition = api.captureSpoolPartition(
        origin,
        principalScope,
        deviceId,
        "capture-session-test",
      );
      const now = Date.now();
      const spoolA = new api.IndexedDbCaptureUploadSpool(databaseName);
      const spoolB = new api.IndexedDbCaptureUploadSpool(databaseName);
      for (let sequence = 0; sequence < 4; sequence += 1) {
        await spoolA.enqueue({
          partition,
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId,
            captureSessionId: "capture-session-test",
            source: "desktop",
          },
          segmentId: `segment-${sequence}`,
          idempotencyKey: `capture-segment-${sequence}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        }, now);
      }
      let active = 0;
      let maxActive = 0;
      let acknowledged = 0;
      const attempts = [];
      let resolveDone = () => undefined;
      const done = new Promise((resolve) => {
        resolveDone = resolve;
      });
      const handlers = {
        upload: async (item) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          attempts.push(item.segmentId);
          await Promise.resolve();
          active -= 1;
        },
        onAcknowledged: () => {
          acknowledged += 1;
          if (acknowledged === 4) resolveDone();
        },
      };
      const coordinatorA = new api.CaptureUploadCoordinator(spoolA, handlers);
      const coordinatorB = new api.CaptureUploadCoordinator(spoolB, handlers);
      coordinatorA.start(partition);
      coordinatorB.start(partition);
      await done;
      coordinatorA.stop();
      coordinatorB.stop();
      const snapshot = await spoolA.snapshot(partition, now);
      return {
        maxActive,
        attempts: attempts.length,
        uniqueAttempts: new Set(attempts).size,
        remaining: snapshot.depth,
      };
    });
    assert.deepEqual(result, {
      maxActive: 1,
      attempts: 4,
      uniqueAttempts: 4,
      remaining: 0,
    });
  });
});

test("浏览器调度取消不把 coordinator 当成 Web API receiver", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const now = Date.now();
      const origin = "https://capture.example";
      const principalScope = "scope-test";
      const deviceId = "device-test";
      const captureSessionId = "capture-session-test";
      const partition = api.captureSpoolPartition(
        origin,
        principalScope,
        deviceId,
        captureSessionId,
      );
      const spool = new api.MemoryCaptureUploadSpool();
      await spool.enqueue({
        partition,
        origin,
        principalScope,
        wav: new Blob([new Uint8Array(4)]),
        byteSize: 4,
        capturedAtMs: now,
        expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
        meetingId: null,
        scope: { deviceId, captureSessionId, source: "desktop" },
        segmentId: "segment-scheduled-cancel",
        idempotencyKey: "capture-scheduled-cancel",
        retryCount: 0,
        nextAttemptAtMs: now + 1_000,
      }, now);

      let markScheduled = () => undefined;
      const scheduled = new Promise((resolve) => {
        markScheduled = resolve;
      });
      const coordinator = new api.CaptureUploadCoordinator(
        spool,
        { upload: async () => undefined },
        {
          now: () => now,
          schedule: () => {
            markScheduled();
            return 1;
          },
          cancelSchedule: function () {
            if (this !== undefined && this !== globalThis) {
              throw new TypeError("scheduler receiver must not be rebound");
            }
          },
        },
      );
      try {
        coordinator.start(partition);
        await scheduled;
        coordinator.kick();
        return { kick: "ok" };
      } finally {
        coordinator.stop();
      }
    });
    assert.deepEqual(result, { kick: "ok" });
  });
});

test("IndexedDB versionchange 后同一 spool 实例可重新打开", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-reopen-test-${crypto.randomUUID()}`;
      const origin = "https://capture.example";
      const principalScope = "scope-test";
      const deviceId = "device-test";
      const partition = api.captureSpoolPartition(
        origin,
        principalScope,
        deviceId,
        "capture-session-test",
      );
      const now = Date.now();
      const spool = new api.IndexedDbCaptureUploadSpool(databaseName);
      await spool.enqueue({
        partition,
        origin,
        principalScope,
        wav: new Blob([new Uint8Array(4)]),
        byteSize: 4,
        capturedAtMs: now,
        expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
        meetingId: null,
        scope: {
          deviceId,
          captureSessionId: "capture-session-test",
          source: "desktop",
        },
        segmentId: "segment-reopen",
        idempotencyKey: "capture-segment-reopen",
        retryCount: 0,
        nextAttemptAtMs: now,
      }, now);
      const currentVersion = await new Promise((resolve, reject) => {
        const current = indexedDB.open(databaseName);
        current.onsuccess = () => {
          const version = current.result.version;
          current.result.close();
          resolve(version);
        };
        current.onerror = () => reject(current.error);
      });
      await new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName, currentVersion + 1);
        request.onupgradeneeded = () => undefined;
        request.onsuccess = () => {
          request.result.close();
          resolve();
        };
        request.onerror = () => reject(request.error);
      });
      const snapshot = await spool.snapshot(partition, now);
      return { depth: snapshot.depth, globalDepth: snapshot.globalDepth };
    });
    assert.deepEqual(result, { depth: 1, globalDepth: 1 });
  });
});

test("不响应 abort 的旧 uploader 也会释放 Web Lock 供新 coordinator 恢复", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-abort-test-${crypto.randomUUID()}`;
      const origin = "https://capture.example";
      const principalScope = "scope-test";
      const deviceId = "device-test";
      const partition = api.captureSpoolPartition(
        origin,
        principalScope,
        deviceId,
        "capture-session-test",
      );
      const now = Date.now();
      const spool = new api.IndexedDbCaptureUploadSpool(databaseName);
      await spool.enqueue({
        partition,
        origin,
        principalScope,
        wav: new Blob([new Uint8Array(4)]),
        byteSize: 4,
        capturedAtMs: now,
        expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
        meetingId: null,
        scope: {
          deviceId,
          captureSessionId: "capture-session-test",
          source: "desktop",
        },
        segmentId: "segment-abort",
        idempotencyKey: "capture-segment-abort",
        retryCount: 0,
        nextAttemptAtMs: now,
      }, now);
      let resolveStarted = () => undefined;
      const started = new Promise((resolve) => {
        resolveStarted = resolve;
      });
      const stuck = new api.CaptureUploadCoordinator(spool, {
        onAttempt: resolveStarted,
        upload: () => new Promise(() => undefined),
      });
      stuck.start(partition);
      await started;
      stuck.stop();

      let resolveRecovered = () => undefined;
      const recovered = new Promise((resolve) => {
        resolveRecovered = resolve;
      });
      let uploads = 0;
      const replacement = new api.CaptureUploadCoordinator(spool, {
        upload: async () => {
          uploads += 1;
        },
        onAcknowledged: resolveRecovered,
      });
      replacement.start(partition);
      await recovered;
      replacement.stop();
      const snapshot = await spool.snapshot(partition, now);
      return { uploads, remaining: snapshot.depth };
    });
    assert.deepEqual(result, { uploads: 1, remaining: 0 });
  });
});

test("真实 IndexedDB/WebLock 使用 active 三路加一路恢复请求并为新实时分区让槽", async () => {
  await withBrowser(async (page) => {
    const result = await page.evaluate(async () => {
      const api = globalThis.CaptureUploadTest;
      const databaseName = `capture-pool-test-${crypto.randomUUID()}`;
      const origin = "https://capture.example";
      const principalScope = "scope-test";
      const now = Date.now();
      const writer = new api.IndexedDbCaptureUploadSpool(databaseName);
      const partitions = [];
      for (let sequence = 0; sequence < 6; sequence += 1) {
        const deviceId = `device-${sequence}`;
        const partition = api.captureSpoolPartition(
          origin,
          principalScope,
          deviceId,
          "capture-session-test",
        );
        partitions.push(partition);
        await writer.enqueue({
          partition,
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId,
            captureSessionId: "capture-session-test",
            source: "desktop",
          },
          segmentId: `segment-${sequence}`,
          idempotencyKey: `capture-segment-${sequence}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        }, now);
      }
      for (let extra = 0; extra < 7; extra += 1) {
        await writer.enqueue({
          partition: partitions[0],
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId: "device-0",
            captureSessionId: "capture-session-test",
            source: "desktop",
          },
          segmentId: `segment-active-${extra + 2}`,
          idempotencyKey: `capture-segment-active-${extra + 2}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        }, now);
      }
      for (let extra = 0; extra < 7; extra += 1) {
        await writer.enqueue({
          partition: partitions[1],
          origin,
          principalScope,
          wav: new Blob([new Uint8Array(4)]),
          byteSize: 4,
          capturedAtMs: now,
          expiresAtMs: now + api.CAPTURE_SPOOL_TTL_MS,
          meetingId: null,
          scope: {
            deviceId: "device-1",
            captureSessionId: "capture-session-test",
            source: "desktop",
          },
          segmentId: `segment-recovery-${extra + 2}`,
          idempotencyKey: `capture-segment-recovery-${extra + 2}`,
          retryCount: 0,
          nextAttemptAtMs: now,
        }, now);
      }

      // 新实例模拟 renderer/App 重启，恢复池必须从 durable DB 发现全部分区。
      const reloaded = new api.IndexedDbCaptureUploadSpool(databaseName);
      let active = 0;
      let maxActive = 0;
      const attempts = [];
      let resolveInitial = () => undefined;
      const initial = new Promise((resolve) => {
        resolveInitial = resolve;
      });
      let resolveSwitched = () => undefined;
      const switched = new Promise((resolve) => {
        resolveSwitched = resolve;
      });
      const pool = new api.CaptureUploadPool(reloaded, {
        upload: (item, signal) => new Promise((_resolve, reject) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          attempts.push(item.partition);
          if (active === 4) resolveInitial();
          if (item.partition === partitions[5]) resolveSwitched();
          signal.addEventListener("abort", () => {
            active -= 1;
            reject(signal.reason);
          }, { once: true });
        }),
      });
      pool.setActivePartition(partitions[0]);
      pool.start();
      await initial;
      const initialHasActive = attempts.includes(partitions[0]);
      const initialUniquePartitions = new Set(attempts).size;
      const initialActiveAttempts = attempts.filter(
        (partition) => partition === partitions[0],
      ).length;
      pool.setActivePartition(partitions[5]);
      await switched;
      const switchedHasActive = attempts.includes(partitions[5]);
      pool.dispose();
      return {
        initialHasActive,
        initialUniquePartitions,
        initialActiveAttempts,
        switchedHasActive,
        maxActive,
      };
    });
    assert.deepEqual(result, {
      initialHasActive: true,
      initialUniquePartitions: 2,
      initialActiveAttempts: 3,
      switchedHasActive: true,
      maxActive: 4,
    });
  });
});
