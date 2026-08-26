import assert from "node:assert/strict";
import test from "node:test";
import {
  DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS,
  endFormalMeetingLifecycle,
  isFormalCaptureAttachmentCurrent,
  requestFormalMeeting,
  REQUEST_FORMAL_MEETING_EVENT,
  startFormalMeetingLifecycle,
  type FormalMeetingSnapshot,
} from "./meetingStartLifecycle.ts";

test("formal quiesce timeout is bounded but covers the normal remote STT tail", () => {
  assert.equal(DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS, 240_000);
});

const active: FormalMeetingSnapshot = {
  mode: "in_meeting",
  meeting_id: "meeting-1",
  started_at: "2026-07-29T00:00:00.000Z",
  started_by: "manual",
};

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("manual start activates before a 35s-slow capture and subsequent enqueue sees the meeting", async () => {
  const slowCapture = deferred<void>();
  const events: string[] = [];
  let overlay: string | null = null;
  let enqueuedMeetingId: string | null = null;

  const started = await startFormalMeetingLifecycle({
    manualStart: async () => {
      events.push("manualStart");
      return active;
    },
    isCurrent: () => true,
    nextCaptureGeneration: () => 7,
    activate: (snapshot) => {
      overlay = snapshot.meeting_id;
      events.push("active");
    },
    prepareCapture: async () => {
      events.push("capturePending");
      await slowCapture.promise;
      // The capture router freezes the formal overlay when it enqueues a chunk.
      enqueuedMeetingId = overlay;
    },
    onCaptureError: () => assert.fail("slow capture must not reject"),
  });

  assert.equal(started, true);
  assert.deepEqual(events, ["manualStart", "active", "capturePending"]);
  assert.equal(overlay, "meeting-1");
  slowCapture.resolve();
  await Promise.resolve();
  assert.equal(enqueuedMeetingId, "meeting-1");
});

test("capture rejection keeps the committed meeting active and manual stop remains available", async () => {
  const captureRejected = deferred<void>();
  const errors: unknown[] = [];
  let overlay: string | null = null;
  let generation = 3;

  await startFormalMeetingLifecycle({
    manualStart: async () => active,
    isCurrent: () => true,
    nextCaptureGeneration: () => generation,
    activate: (snapshot) => { overlay = snapshot.meeting_id; },
    prepareCapture: () => captureRejected.promise,
    onCaptureError: (error) => errors.push(error),
  });
  captureRejected.reject(new Error("microphone unavailable"));
  await Promise.resolve();
  assert.equal(overlay, "meeting-1");
  assert.equal(errors.length, 1);

  const stopped = await endFormalMeetingLifecycle({
    manualEnd: async () => ({ ...active, mode: "idle", meeting_id: null }),
    isCurrent: () => true,
    stopCaptureProducer: () => undefined,
    awaitCaptureRouterDrain: async () => undefined,
    awaitFormalPartitionIdle: async () => undefined,
    restoreCapture: () => undefined,
    canRestoreCapture: async () => true,
    invalidateCapture: () => { generation += 1; },
    deactivate: () => { overlay = null; },
  });
  assert.equal(stopped, true);
  assert.equal(overlay, null);
});

test("formal stop awaits producer, router drain, and partition idle before manualEnd", async () => {
  const events: string[] = [];
  const stopped = await endFormalMeetingLifecycle({
    manualEnd: async () => {
      events.push("manualEnd");
      return { ...active, mode: "idle", meeting_id: null };
    },
    isCurrent: () => true,
    stopCaptureProducer: () => events.push("stopProducer"),
    awaitCaptureRouterDrain: async () => events.push("routerDrain"),
    awaitFormalPartitionIdle: async () => events.push("partitionIdle"),
    restoreCapture: () => events.push("restore"),
    canRestoreCapture: async () => true,
    invalidateCapture: () => events.push("invalidate"),
    deactivate: () => events.push("deactivate"),
  });
  assert.equal(stopped, true);
  assert.deepEqual(events, [
    "stopProducer",
    "invalidate",
    "routerDrain",
    "partitionIdle",
    "manualEnd",
    "deactivate",
    "restore",
  ]);
});

test("formal stop timeout never calls manualEnd and restores the active overlay", async () => {
  const events: string[] = [];
  const errors: unknown[] = [];
  const stopped = await endFormalMeetingLifecycle({
    manualEnd: async () => {
      events.push("manualEnd");
      return { ...active, mode: "idle", meeting_id: null };
    },
    isCurrent: () => true,
    stopCaptureProducer: () => events.push("stopProducer"),
    awaitCaptureRouterDrain: () => new Promise<void>(() => undefined),
    awaitFormalPartitionIdle: async () => events.push("partitionIdle"),
    restoreCapture: () => events.push("restore"),
    canRestoreCapture: async () => true,
    invalidateCapture: () => events.push("invalidate"),
    deactivate: () => events.push("deactivate"),
    onStopError: (error) => errors.push(error),
    quiesceTimeoutMs: 10,
  });
  assert.equal(stopped, false);
  assert.deepEqual(events, ["stopProducer", "invalidate", "restore"]);
  assert.equal(errors.length, 1);
});

test("formal stop timeout after watchdog end does not restore producer", async () => {
  const events: string[] = [];
  const errors: unknown[] = [];
  const stopped = await endFormalMeetingLifecycle({
    manualEnd: async () => {
      events.push("manualEnd");
      return { ...active, mode: "idle", meeting_id: null };
    },
    isCurrent: () => true,
    stopCaptureProducer: () => events.push("stopProducer"),
    awaitCaptureRouterDrain: () => new Promise<void>(() => undefined),
    awaitFormalPartitionIdle: async () => undefined,
    restoreCapture: () => events.push("restore"),
    canRestoreCapture: async () => false,
    onTerminalCapture: () => events.push("terminal"),
    invalidateCapture: () => events.push("invalidate"),
    deactivate: () => events.push("deactivate"),
    onStopError: (error) => errors.push(error),
    quiesceTimeoutMs: 10,
  });

  assert.equal(stopped, false);
  assert.deepEqual(events, ["stopProducer", "invalidate", "terminal"]);
  assert.deepEqual(errors, []);
});

test("stop before capture readiness rejects a late formal attachment", () => {
  let currentGeneration = 11;
  const requestedGeneration = currentGeneration;
  assert.equal(
    isFormalCaptureAttachmentCurrent(currentGeneration, requestedGeneration, "meeting-1"),
    true,
  );
  currentGeneration += 1; // stop invalidates before it awaits the end API
  assert.equal(
    isFormalCaptureAttachmentCurrent(currentGeneration, requestedGeneration, null),
    false,
  );
});

test("manual start API rejection never activates an overlay", async () => {
  let activated = false;
  let captureLaunched = false;
  await assert.rejects(
    startFormalMeetingLifecycle({
      manualStart: async () => { throw new Error("start rejected"); },
      isCurrent: () => true,
      nextCaptureGeneration: () => 1,
      activate: () => { activated = true; },
      prepareCapture: async () => { captureLaunched = true; },
      onCaptureError: () => assert.fail("capture must not launch"),
    }),
  );
  assert.equal(activated, false);
  assert.equal(captureLaunched, false);
});

test("minutes empty-state request uses the shared formal meeting event", () => {
  const events: string[] = [];
  const previousWindow = (globalThis as { window?: unknown }).window;
  (globalThis as { window?: { dispatchEvent: (event: Event) => void } }).window = {
    dispatchEvent: (event) => events.push(event.type),
  };
  try {
    requestFormalMeeting();
    assert.deepEqual(events, [REQUEST_FORMAL_MEETING_EVENT]);
  } finally {
    if (previousWindow === undefined) delete (globalThis as { window?: unknown }).window;
    else (globalThis as { window?: unknown }).window = previousWindow;
  }
});
