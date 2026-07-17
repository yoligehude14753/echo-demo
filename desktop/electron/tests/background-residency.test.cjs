"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  DEFAULT_BACKGROUND_STATUS,
  normalizeBackgroundStatus,
  formalMeetingStatusLabel,
  captureStatusLabel,
} = require("../background-residency.cjs");

test("accepts the shared renderer capture runtime contract", () => {
  const status = normalizeBackgroundStatus({
    version: 1,
    state: "formal_recording",
    freeModeEnabled: true,
    formalMeetingId: "meeting-1",
    selected: true,
    errorMessage: null,
  });
  assert.equal(formalMeetingStatusLabel(status), "正式会议：进行中");
  assert.equal(captureStatusLabel(status), "自由收音：正式会议收音中");
});

test("free listening remains separate from formal meeting state", () => {
  const status = normalizeBackgroundStatus({
    version: 1,
    state: "free_listening",
    freeModeEnabled: true,
    formalMeetingId: null,
    selected: true,
    errorMessage: null,
  });
  assert.equal(formalMeetingStatusLabel(status), "正式会议：未开始");
  assert.equal(captureStatusLabel(status), "自由收音：持续监听");
});

test("offline buffering and errors remain explicit", () => {
  assert.equal(
    captureStatusLabel(normalizeBackgroundStatus({
      version: 1,
      state: "offline_buffering",
      freeModeEnabled: true,
      formalMeetingId: null,
      selected: true,
      errorMessage: null,
    })),
    "自由收音：离线缓存中",
  );
  assert.equal(
    captureStatusLabel(normalizeBackgroundStatus({
      version: 1,
      state: "error",
      freeModeEnabled: true,
      formalMeetingId: null,
      selected: true,
      errorMessage: "device unavailable",
    })),
    "自由收音：异常（device unavailable）",
  );
});

test("untrusted values fail closed", () => {
  assert.deepEqual(
    normalizeBackgroundStatus({
      version: 2,
      state: "capturing-ish",
    }),
    DEFAULT_BACKGROUND_STATUS,
  );
});
