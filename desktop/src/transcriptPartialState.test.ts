import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  applyTranscriptPartialEvent,
  clearTranscriptPartial,
  visibleTranscriptPartials,
} from "./transcriptPartialState.ts";

const baseEvent = {
  type: "transcript.partial" as const,
  seq: 1,
  ts: "2026-08-09T00:00:00.000Z",
  meeting_id: "meeting-1",
  payload: {
    correlation: "capture-0123456789abcdef",
    text: "流",
    state: "partial",
  },
};

test("partial transcript replaces text in place and preserves its first timestamp", () => {
  const first = applyTranscriptPartialEvent({}, baseEvent);
  const second = applyTranscriptPartialEvent(first, {
    ...baseEvent,
    seq: 2,
    ts: "2026-08-09T00:00:01.000Z",
    payload: { ...baseEvent.payload, text: "流式" },
  });

  assert.equal(Object.keys(second).length, 1);
  assert.equal(second[baseEvent.payload.correlation].text, "流式");
  assert.equal(
    second[baseEvent.payload.correlation].capturedAt,
    baseEvent.ts,
  );
});

test("failed events and canonical completion clear only the matching projection", () => {
  const current = applyTranscriptPartialEvent({}, baseEvent);
  const failed = applyTranscriptPartialEvent(current, {
    ...baseEvent,
    payload: { ...baseEvent.payload, text: "", state: "failed" },
  });
  assert.deepEqual(failed, {});

  const restored = applyTranscriptPartialEvent({}, baseEvent);
  assert.deepEqual(
    clearTranscriptPartial(restored, baseEvent.payload.correlation),
    {},
  );
});

test("meeting selection never leaks another meeting's partial text", () => {
  const first = applyTranscriptPartialEvent({}, baseEvent);
  const ambient = applyTranscriptPartialEvent(first, {
    ...baseEvent,
    seq: 2,
    meeting_id: null,
    payload: {
      correlation: "capture-fedcba9876543210",
      text: "ambient",
      state: "partial",
    },
  });

  assert.deepEqual(
    visibleTranscriptPartials(ambient, "meeting-1").map((item) => item.text),
    ["流"],
  );
  assert.deepEqual(
    visibleTranscriptPartials(ambient, null).map((item) => item.text),
    ["ambient"],
  );
});

test("store, upload receipt, and transcript view keep partials transient", () => {
  const store = readFileSync(new URL("./store.ts", import.meta.url), "utf8");
  const router = readFileSync(
    new URL("./capture/captureChunkRouter.ts", import.meta.url),
    "utf8",
  );
  const transcript = readFileSync(
    new URL("./components/TranscriptStream.tsx", import.meta.url),
    "utf8",
  );

  assert.match(store, /case "transcript\.partial"/);
  assert.match(store, /completeTranscriptPartial\(seg\.capture_correlation\)/);
  assert.match(
    router,
    /completeTranscriptPartial\(result\.admission\.receipt_id\)/,
  );
  assert.match(transcript, /visibleTranscriptPartials/);
  assert.match(transcript, /key=\{s\.stableKey/);
  assert.match(transcript, /aria-live=\{isPartial \? "polite"/);
  assert.match(transcript, /getMeetingTranscript\(activeMeetingId/);
  assert.match(transcript, /hydrateMeetingSegments\(activeMeetingId, segments\)/);
  assert.doesNotMatch(store, /transcriptPartials:\s*state\.transcriptPartials/);
});
