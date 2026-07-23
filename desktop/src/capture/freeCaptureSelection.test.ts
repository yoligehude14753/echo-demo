import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { planFreeCaptureSelection } from "./freeCaptureSelection.ts";

const local = {
  deviceId: "device-mac",
  deviceName: "本机 Mac",
  platform: "macos",
  online: true,
};

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
