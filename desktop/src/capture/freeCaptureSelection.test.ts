import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  chooseCaptureDeviceId,
  planFreeCaptureSelection,
  resolveCaptureSetupPolicy,
} from "./freeCaptureSelection.ts";

const local = {
  deviceId: "device-mac",
  deviceName: "本机 Mac",
  platform: "macos",
  online: true,
};

test("identity error with granted microphone enters local device setup", () => {
  assert.deepEqual(
    resolveCaptureSetupPolicy({
      identityPhase: "error",
      identityErrorCode: "IDENTITY_ERROR",
      micStatus: "granted",
      selected: false,
    }),
    { kind: "select_device", authority: "local" },
  );
});

test("microphone denial remains a hard local capture gate", () => {
  assert.deepEqual(
    resolveCaptureSetupPolicy({
      identityPhase: "error",
      identityErrorCode: "IDENTITY_ERROR",
      micStatus: "denied",
      selected: false,
    }),
    { kind: "permission_required" },
  );
});

test("valid identity still requires an explicit backend device selection", () => {
  assert.deepEqual(
    resolveCaptureSetupPolicy({
      identityPhase: "ready",
      identityErrorCode: null,
      micStatus: "granted",
      selected: false,
    }),
    { kind: "select_device", authority: "backend" },
  );
});

test("valid identity and selected device preserve backend authorization", () => {
  assert.deepEqual(
    resolveCaptureSetupPolicy({
      identityPhase: "ready",
      identityErrorCode: null,
      micStatus: "granted",
      selected: true,
    }),
    { kind: "ready", authority: "backend" },
  );
});

test("free capture automatically claims the sole local online device", () => {
  assert.deepEqual(planFreeCaptureSelection([local], {
    sessionDeviceId: local.deviceId,
    localDeviceId: local.deviceId,
  }), {
    kind: "auto_single",
    deviceId: local.deviceId,
  });
});

test("an unpaired authenticated self is selected when the remote list is empty", () => {
  assert.deepEqual(planFreeCaptureSelection([], {
    sessionDeviceId: local.deviceId,
    localDeviceId: local.deviceId,
  }), {
    kind: "auto_single",
    deviceId: local.deviceId,
  });
});

test("paired remote candidates require an explicit choice instead of silent multi-capture", () => {
  const plan = planFreeCaptureSelection(
    [{ ...local, deviceId: "device-room", deviceName: "会议室 Mac" }],
    {
      sessionDeviceId: local.deviceId,
      localDeviceId: local.deviceId,
    },
  );
  assert.equal(plan.kind, "choose");
  if (plan.kind === "choose") {
    assert.deepEqual(plan.devices.map((device) => device.deviceId), [
      local.deviceId,
      "device-room",
    ]);
  }
});

test("a renderer device id that differs from the session principal never becomes an owner", () => {
  assert.deepEqual(
    planFreeCaptureSelection([], {
      sessionDeviceId: "device-session",
      localDeviceId: local.deviceId,
    }),
    { kind: "identity_mismatch" },
  );
});

test("multiple devices auto-select only an explicit browser default candidate", () => {
  const plan = planFreeCaptureSelection(
    [{ ...local, deviceId: "device-room" }],
    { sessionDeviceId: local.deviceId, localDeviceId: local.deviceId },
  );
  assert.equal(chooseCaptureDeviceId(plan, { browserDefaultAudioInput: true }), local.deviceId);
  assert.equal(chooseCaptureDeviceId(plan, { browserDefaultAudioInput: false }), null);
});

test("explicit device selection overrides the automatic default", () => {
  const plan = planFreeCaptureSelection(
    [{ ...local, deviceId: "device-room" }],
    { sessionDeviceId: local.deviceId, localDeviceId: local.deviceId },
  );
  assert.equal(
    chooseCaptureDeviceId(plan, {
      browserDefaultAudioInput: true,
      explicitDeviceId: "device-room",
    }),
    "device-room",
  );
});

test("without literal default, a real default track match is deterministic", () => {
  const plan = planFreeCaptureSelection(
    [{ ...local, deviceId: "device-room" }],
    { sessionDeviceId: local.deviceId, localDeviceId: local.deviceId },
  );
  assert.equal(
    chooseCaptureDeviceId(plan, {
      browserDefaultAudioInput: false,
      browserDefaultTrackDeviceId: local.deviceId,
    }),
    local.deviceId,
  );
});

test("without literal default or candidate match, automatic selection stays picker", () => {
  const plan = planFreeCaptureSelection(
    [{ ...local, deviceId: "device-room" }],
    { sessionDeviceId: local.deviceId, localDeviceId: local.deviceId },
  );
  assert.equal(
    chooseCaptureDeviceId(plan, {
      browserDefaultAudioInput: false,
      browserDefaultTrackDeviceId: "browser-only-id",
    }),
    null,
  );
});

test("no authenticated device produces no automatic selection", () => {
  const plan = planFreeCaptureSelection([], {
    sessionDeviceId: null,
    localDeviceId: local.deviceId,
  });
  assert.equal(chooseCaptureDeviceId(plan, { browserDefaultAudioInput: true }), null);
});
