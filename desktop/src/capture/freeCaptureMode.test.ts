import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  beginFreeCaptureSetup,
  currentFreeCaptureSetupSnapshot,
  deriveCaptureRuntimeState,
  finishFreeCaptureSetup,
  onFreeCaptureSetupRequest,
  requestFreeCaptureSetup,
  resetFreeCaptureSetupForTest,
  resolveFreeCapturePreference,
  retryFreeCaptureSetup,
} from "./freeCaptureMode.ts";

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

test("missing microphone permission fails closed without claiming a formal recording", () => {
  const runtimeState = deriveCaptureRuntimeState({
    ...base,
    captureState: "error",
    errorMessage: "NotAllowedError: Permission denied",
    formalMeetingId: "m-formal",
  });
  assert.equal(runtimeState, "permission_required");
  assert.notEqual(runtimeState, "formal_recording");
  const status = readFileSync(
    new URL("../components/CaptureStatus.tsx", import.meta.url),
    "utf8",
  );
  assert.match(status, /打开系统麦克风设置/);
  assert.match(status, /openMicSystemPrefs/);
});

test("missing preference defaults on without erasing an explicit pause", () => {
  assert.deepEqual(resolveFreeCapturePreference(null), {
    configured: false,
    enabled: true,
  });
  assert.deepEqual(resolveFreeCapturePreference("1"), {
    configured: true,
    enabled: true,
  });
  assert.deepEqual(resolveFreeCapturePreference("0"), {
    configured: true,
    enabled: false,
  });
});

test("a pending setup request is replayed when its listener mounts later", () => {
  resetFreeCaptureSetupForTest();
  const requested = requestFreeCaptureSetup("first_run");
  const received: typeof requested[] = [];

  const off = onFreeCaptureSetupRequest((snapshot) => received.push(snapshot));
  assert.deepEqual(received, [requested]);
  assert.equal(beginFreeCaptureSetup(requested.requestId!), true);
  assert.equal(beginFreeCaptureSetup(requested.requestId!), false);
  assert.equal(currentFreeCaptureSetupSnapshot().state, "running");
  off();
});

test("a session recovery can redeliver setup once and then fails closed", () => {
  resetFreeCaptureSetupForTest();
  const received: string[] = [];
  const off = onFreeCaptureSetupRequest((snapshot) => received.push(snapshot.state));
  const requested = requestFreeCaptureSetup("first_run");
  const requestId = requested.requestId!;

  assert.equal(beginFreeCaptureSetup(requestId), true);
  finishFreeCaptureSetup(requestId, "retryable_failed", "session pending");
  assert.equal(currentFreeCaptureSetupSnapshot().state, "retryable_failed");
  assert.equal(retryFreeCaptureSetup(requestId), true);
  assert.equal(beginFreeCaptureSetup(requestId), true);
  finishFreeCaptureSetup(requestId, "retryable_failed", "session pending");

  assert.deepEqual(received, ["pending", "pending"]);
  assert.deepEqual(currentFreeCaptureSetupSnapshot(), {
    requestId,
    reason: "first_run",
    attempt: 1,
    state: "failed",
    errorMessage: "session pending",
  });
  assert.equal(retryFreeCaptureSetup(requestId), false);
  off();
});

test("automatic setup preserves the established control and audio gates", () => {
  const status = readFileSync(
    new URL("../components/MeetingStatusBar.tsx", import.meta.url),
    "utf8",
  );
  assert.match(status, /await ensureServerSession\(\);\s+const result = await prepareCapture/);
  assert.match(status, /const control = await updateCaptureControl/);
  assert.match(status, /announceCaptureControl\(control\);/);
  assert.match(status, /await audioCapture\.waitForFirstFrame\(\);/);

  const android = readFileSync(
    new URL("./AndroidCaptureSelector.tsx", import.meta.url),
    "utf8",
  );
  assert.match(android, /beginFreeCaptureSetup\(setup\.requestId\)/);
  assert.match(android, /authorizeCaptureDevice\(localDeviceId, saved\.revision\)/);
  assert.match(android, /finishFreeCaptureSetup\(/);
});
