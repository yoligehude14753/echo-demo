export const FREE_CAPTURE_ENABLED_KEY = "echodesk.capture.freeModeEnabled.v1";
export const FREE_CAPTURE_CHANGE_EVENT = "echodesk:free-capture-change";
export const FREE_CAPTURE_RUNTIME_EVENT = "echodesk:capture-runtime-state";
export const FREE_CAPTURE_COMMAND_EVENT = "echodesk:free-capture-command";

export type CaptureRuntimeState =
  | "off"
  | "permission_required"
  | "device_not_selected"
  | "free_starting"
  | "free_listening"
  | "speech_detected"
  | "formal_recording"
  | "offline_buffering"
  | "error";

export interface CaptureRuntimeSnapshot {
  version: 1;
  state: CaptureRuntimeState;
  freeModeEnabled: boolean;
  formalMeetingId: string | null;
  selected: boolean;
  errorMessage: string | null;
}

export type FreeCaptureCommand = "pause" | "resume";

export interface CaptureRuntimeInputs {
  freeModeEnabled: boolean;
  selected: boolean;
  captureState: "standby" | "initializing" | "capturing" | "error";
  formalMeetingId: string | null;
  uploadUnavailable: boolean;
  speechDetected: boolean;
  errorMessage: string | null;
}

export function deriveCaptureRuntimeState(
  input: CaptureRuntimeInputs,
): CaptureRuntimeState {
  if (!input.freeModeEnabled) return "off";
  if (!input.selected) return "device_not_selected";
  if (
    input.captureState === "error" &&
    /permission denied|notallowederror|denied/i.test(input.errorMessage ?? "")
  ) {
    return "permission_required";
  }
  if (input.captureState === "error") return "error";
  if (input.captureState !== "capturing") return "free_starting";
  if (input.uploadUnavailable) return "offline_buffering";
  if (input.formalMeetingId) return "formal_recording";
  if (input.speechDetected) return "speech_detected";
  return "free_listening";
}

let formalMeetingId: string | null = null;

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function isFreeCaptureEnabled(): boolean {
  return storage()?.getItem(FREE_CAPTURE_ENABLED_KEY) === "1";
}

export function setFreeCaptureEnabled(enabled: boolean): void {
  storage()?.setItem(FREE_CAPTURE_ENABLED_KEY, enabled ? "1" : "0");
  window.dispatchEvent(
    new CustomEvent<boolean>(FREE_CAPTURE_CHANGE_EVENT, { detail: enabled }),
  );
}

export function onFreeCaptureChange(listener: (enabled: boolean) => void): () => void {
  const handler = () => listener(isFreeCaptureEnabled());
  window.addEventListener(FREE_CAPTURE_CHANGE_EVENT, handler);
  return () => window.removeEventListener(FREE_CAPTURE_CHANGE_EVENT, handler);
}

export function setFormalMeetingOverlay(meetingId: string | null): void {
  formalMeetingId = meetingId;
  window.dispatchEvent(new Event(FREE_CAPTURE_CHANGE_EVENT));
}

export function currentFormalMeetingOverlay(): string | null {
  return formalMeetingId;
}

export function publishCaptureRuntime(snapshot: CaptureRuntimeSnapshot): void {
  document.documentElement.dataset.captureRuntimeState = snapshot.state;
  window.dispatchEvent(
    new CustomEvent<CaptureRuntimeSnapshot>(FREE_CAPTURE_RUNTIME_EVENT, {
      detail: snapshot,
    }),
  );
  window.echo?.notifyCaptureState?.(snapshot);
}

export function installFreeCaptureCommandBridge(): () => void {
  const apply = (command: FreeCaptureCommand) =>
    setFreeCaptureEnabled(command === "resume");
  const onDomCommand = (event: Event) => {
    const command = (event as CustomEvent<{ command?: unknown }>).detail?.command;
    if (command === "pause" || command === "resume") apply(command);
  };
  window.addEventListener(FREE_CAPTURE_COMMAND_EVENT, onDomCommand);
  const offIpc = window.echo?.onCaptureCommand?.(apply);
  return () => {
    window.removeEventListener(FREE_CAPTURE_COMMAND_EVENT, onDomCommand);
    offIpc?.();
  };
}
