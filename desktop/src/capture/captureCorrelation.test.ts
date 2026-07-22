import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
// @ts-expect-error Node strip-types requires the explicit source extension.
import { captureSegmentCorrelation, normalizeAmbientSegments } from "./captureCorrelation.ts";

test("chunk and recent results produce the same opaque correlation", () => {
  const chunkResponse = {
    segment_id: "device-secret:42:segment-uuid",
    ambient_stored: true,
    ambient_text: "同一段转写",
  };
  const [recent] = normalizeAmbientSegments([
    {
      text: chunkResponse.ambient_text,
      captured_at: "2026-07-23T00:00:00.000Z",
      speaker_id: null,
      speaker_label: null,
      duration_ms: 1200,
      segment_id: chunkResponse.segment_id,
    },
  ]);

  const chunkCorrelation = captureSegmentCorrelation(chunkResponse.segment_id);
  assert.equal(recent.segment_correlation, chunkCorrelation);
  assert.match(recent.segment_correlation ?? "", /^seg-[0-9a-f]{16}$/);
  assert.notEqual(recent.segment_correlation, chunkResponse.segment_id);
  assert.equal("segment_id" in recent, false);
  assert.equal("segment_id" in chunkResponse, true);
  assert.equal("device-secret:42:segment-uuid".includes(recent.segment_correlation ?? ""), false);
});

test("same segment compares equal while different segments stay distinct", () => {
  const first = captureSegmentCorrelation("device-secret:1:one");
  const same = captureSegmentCorrelation("device-secret:1:one");
  const other = captureSegmentCorrelation("device-secret:1:two");

  assert.equal(first, same);
  assert.notEqual(first, other);
  assert.equal(captureSegmentCorrelation(null), null);
});

test("Android WebView exposes only stable correlation and product hooks", () => {
  const transcript = readFileSync(
    new URL("../components/TranscriptStream.tsx", import.meta.url),
    "utf8",
  );
  const selection = readFileSync(
    new URL("./AndroidCaptureSelector.tsx", import.meta.url),
    "utf8",
  );
  const artifacts = readFileSync(
    new URL("../components/ArtifactPanel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(transcript, /data-segment-correlation=/);
  assert.match(selection, /data-capture-selection="surface"/);
  assert.match(selection, /data-capture-selection-option=/);
  assert.match(artifacts, /testId="agent-artifact-link"/);
});
