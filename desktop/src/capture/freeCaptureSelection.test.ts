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
  assert.deepEqual(planFreeCaptureSelection([local], local.deviceId), {
    kind: "auto_single",
    deviceId: local.deviceId,
  });
});

test("free capture requires an explicit choice when multiple devices are online", () => {
  const plan = planFreeCaptureSelection(
    [local, { ...local, deviceId: "device-room", deviceName: "会议室 Mac" }],
    local.deviceId,
  );
  assert.equal(plan.kind, "choose");
  if (plan.kind === "choose") assert.equal(plan.devices.length, 2);
});

test("free capture never claims an unavailable local device", () => {
  assert.deepEqual(
    planFreeCaptureSelection([], local.deviceId),
    { kind: "local_unavailable" },
  );
});
