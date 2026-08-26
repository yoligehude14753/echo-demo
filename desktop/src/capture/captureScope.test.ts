import assert from "node:assert/strict";
import test from "node:test";
// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  isCurrentCaptureAttribution,
  isSameCaptureScope,
  resolveCaptureResponseAttribution,
} from "./captureScope.ts";

const scope = {
  deviceId: "desktop-current",
  captureSessionId: "capture-current",
  source: "desktop" as const,
};

test("capture output admits only the active device, session, and source", () => {
  assert.equal(
    isCurrentCaptureAttribution(
      {
        device_id: scope.deviceId,
        capture_session_id: scope.captureSessionId,
        source: scope.source,
      },
      scope,
    ),
    true,
  );
});

test("active ownership ignores unrelated renderer generations but rejects a stale capture lifetime", () => {
  assert.equal(isSameCaptureScope({ ...scope }, scope), true);
  assert.equal(
    isSameCaptureScope(
      { ...scope, captureSessionId: "capture-old" },
      scope,
    ),
    false,
  );
});

test("capture output fails closed for stale, foreign, or unscoped frames", () => {
  for (const payload of [
    { device_id: "desktop-other", capture_session_id: scope.captureSessionId, source: "desktop" },
    { device_id: scope.deviceId, capture_session_id: "capture-old", source: "desktop" },
    { device_id: scope.deviceId, capture_session_id: scope.captureSessionId, source: "device" },
    { device_id: scope.deviceId, source: "desktop" },
  ]) {
    assert.equal(isCurrentCaptureAttribution(payload, scope), false);
  }
});

test("an entirely unscoped v0.3.2 response is bound only to its request scope", () => {
  const attribution = resolveCaptureResponseAttribution({}, scope);

  assert.equal(isCurrentCaptureAttribution(attribution, scope), true);
  assert.equal(
    isCurrentCaptureAttribution(attribution, {
      ...scope,
      captureSessionId: "capture-replaced",
    }),
    false,
  );
});

test("partial or mismatched response attribution remains fail-closed", () => {
  for (const payload of [
    { device_id: scope.deviceId },
    { capture_session_id: scope.captureSessionId },
    { source: scope.source },
    {
      device_id: null,
      capture_session_id: scope.captureSessionId,
      source: scope.source,
    },
    {
      device_id: "desktop-other",
      capture_session_id: scope.captureSessionId,
      source: scope.source,
    },
  ]) {
    const attribution = resolveCaptureResponseAttribution(payload, scope);
    assert.equal(isCurrentCaptureAttribution(attribution, scope), false);
  }
});
