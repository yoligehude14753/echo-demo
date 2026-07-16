import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { deriveCaptureRuntimeState } from "./freeCaptureMode.ts";

const base = {
  freeModeEnabled: true,
  selected: true,
  captureState: "capturing" as const,
  formalMeetingId: null,
  uploadUnavailable: false,
  speechDetected: false,
  errorMessage: null,
};

test("formal meeting is an overlay over active free capture", () => {
  assert.equal(
    deriveCaptureRuntimeState({ ...base, formalMeetingId: "m-formal" }),
    "formal_recording",
  );
  assert.equal(deriveCaptureRuntimeState(base), "free_listening");
});

test("capture cannot claim a meeting while no audio source is active", () => {
  assert.equal(
    deriveCaptureRuntimeState({
      ...base,
      captureState: "initializing",
      formalMeetingId: "m-formal",
    }),
    "free_starting",
  );
});

test("pause, selection and offline states are explicit", () => {
  assert.equal(
    deriveCaptureRuntimeState({ ...base, freeModeEnabled: false }),
    "off",
  );
  assert.equal(
    deriveCaptureRuntimeState({ ...base, selected: false }),
    "device_not_selected",
  );
  assert.equal(
    deriveCaptureRuntimeState({ ...base, uploadUnavailable: true }),
    "offline_buffering",
  );
});
