"use strict";

const CAPTURE_STATES = new Set([
  "off",
  "permission_required",
  "device_not_selected",
  "free_starting",
  "free_listening",
  "speech_detected",
  "formal_recording",
  "offline_buffering",
  "error",
]);

const DEFAULT_BACKGROUND_STATUS = Object.freeze({
  version: 1,
  state: "off",
  freeModeEnabled: false,
  formalMeetingId: null,
  selected: false,
  errorMessage: null,
});

function normalizeBackgroundStatus(raw) {
  if (raw?.version !== 1 || !CAPTURE_STATES.has(raw?.state)) {
    return DEFAULT_BACKGROUND_STATUS;
  }
  const formalMeetingId =
    typeof raw.formalMeetingId === "string" && raw.formalMeetingId.trim()
      ? raw.formalMeetingId.trim().slice(0, 160)
      : null;
  const errorMessage =
    typeof raw.errorMessage === "string" && raw.errorMessage.trim()
      ? raw.errorMessage.trim().slice(0, 200)
      : null;
  return Object.freeze({
    version: 1,
    state: raw.state,
    freeModeEnabled: raw.freeModeEnabled === true,
    formalMeetingId,
    selected: raw.selected === true,
    errorMessage,
  });
}

function formalMeetingStatusLabel(status) {
  return status.formalMeetingId
    ? "正式会议：进行中"
    : "正式会议：未开始";
}

function captureStatusLabel(status) {
  switch (status.state) {
    case "permission_required":
      return "自由收音：需要麦克风权限";
    case "device_not_selected":
      return "自由收音：本设备未选中";
    case "free_starting":
      return "自由收音：正在启动";
    case "free_listening":
      return "自由收音：持续监听";
    case "speech_detected":
      return "自由收音：检测到语音";
    case "formal_recording":
      return "自由收音：正式会议收音中";
    case "offline_buffering":
      return "自由收音：离线缓存中";
    case "error":
      return status.errorMessage
        ? `自由收音：异常（${status.errorMessage}）`
        : "自由收音：异常";
    default:
      return "自由收音：已暂停";
  }
}

module.exports = {
  DEFAULT_BACKGROUND_STATUS,
  normalizeBackgroundStatus,
  formalMeetingStatusLabel,
  captureStatusLabel,
};
