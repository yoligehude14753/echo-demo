export const FREE_CAPTURE_ENABLED_KEY = "echodesk.capture.freeModeEnabled.v1";
export const FREE_CAPTURE_CHANGE_EVENT = "echodesk:free-capture-change";
export const FREE_CAPTURE_RUNTIME_EVENT = "echodesk:capture-runtime-state";
export const FREE_CAPTURE_COMMAND_EVENT = "echodesk:free-capture-command";
export const FREE_CAPTURE_SETUP_REQUEST_EVENT =
  "echodesk:free-capture-setup-request";
export const FREE_CAPTURE_SETUP_STATE_EVENT =
  "echodesk:free-capture-setup-state";

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
export type FreeCaptureSetupReason = "first_run" | "formal_meeting";

export type FreeCaptureSetupState =
  | "idle"
  | "pending"
  | "running"
  | "awaiting_selection"
  | "succeeded"
  | "retryable_failed"
  | "failed";

export interface FreeCaptureSetupSnapshot {
  requestId: number | null;
  reason: FreeCaptureSetupReason | null;
  attempt: number;
  state: FreeCaptureSetupState;
  errorMessage: string | null;
}

const MAX_FREE_CAPTURE_SETUP_ATTEMPTS = 2;

export interface FreeCapturePreference {
  configured: boolean;
  enabled: boolean;
}

export function resolveFreeCapturePreference(
  value: string | null | undefined,
): FreeCapturePreference {
  if (value === "0") return { configured: true, enabled: false };
  if (value === "1") return { configured: true, enabled: true };
  return { configured: false, enabled: true };
}

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
let latestRuntimeSnapshot: CaptureRuntimeSnapshot | null = null;
let nextFreeCaptureSetupRequestId = 1;
let freeCaptureSetupSnapshot: FreeCaptureSetupSnapshot = {
  requestId: null,
  reason: null,
  attempt: 0,
  state: "idle",
  errorMessage: null,
};
const freeCaptureSetupListeners = new Set<
  (snapshot: FreeCaptureSetupSnapshot) => void
>();

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readFreeCapturePreference(): FreeCapturePreference {
  const value = storage()?.getItem(FREE_CAPTURE_ENABLED_KEY);
  // First-run and upgraded installs default to the product's always-listening
  // intent. This does not bypass device selection or the OS microphone prompt.
  return resolveFreeCapturePreference(value);
}

export function isFreeCaptureEnabled(): boolean {
  return readFreeCapturePreference().enabled;
}

export function isFreeCapturePreferenceConfigured(): boolean {
  return readFreeCapturePreference().configured;
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
  latestRuntimeSnapshot = snapshot;
  document.documentElement.dataset.captureRuntimeState = snapshot.state;
  window.dispatchEvent(
    new CustomEvent<CaptureRuntimeSnapshot>(FREE_CAPTURE_RUNTIME_EVENT, {
      detail: snapshot,
    }),
  );
  window.echo?.notifyCaptureState?.(snapshot);
}

export function currentCaptureRuntimeSnapshot(): CaptureRuntimeSnapshot | null {
  return latestRuntimeSnapshot;
}

export function currentFreeCaptureSetupSnapshot(): FreeCaptureSetupSnapshot {
  return { ...freeCaptureSetupSnapshot };
}

function publishFreeCaptureSetup(): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.freeCaptureSetupState =
      freeCaptureSetupSnapshot.state;
  }
  const snapshot = currentFreeCaptureSetupSnapshot();
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent<FreeCaptureSetupSnapshot>(FREE_CAPTURE_SETUP_STATE_EVENT, {
        detail: snapshot,
      }),
    );
    if (snapshot.state === "pending") {
      window.dispatchEvent(
        new CustomEvent<FreeCaptureSetupSnapshot>(FREE_CAPTURE_SETUP_REQUEST_EVENT, {
          detail: snapshot,
        }),
      );
    }
  }
  if (snapshot.state === "pending") {
    for (const listener of freeCaptureSetupListeners) listener(snapshot);
  }
}

export function requestFreeCaptureSetup(
  reason: FreeCaptureSetupReason = "first_run",
): FreeCaptureSetupSnapshot {
  if (
    freeCaptureSetupSnapshot.state === "pending" ||
    freeCaptureSetupSnapshot.state === "running" ||
    freeCaptureSetupSnapshot.state === "awaiting_selection"
  ) {
    return currentFreeCaptureSetupSnapshot();
  }
  freeCaptureSetupSnapshot = {
    requestId: nextFreeCaptureSetupRequestId++,
    reason,
    attempt: 0,
    state: "pending",
    errorMessage: null,
  };
  publishFreeCaptureSetup();
  return currentFreeCaptureSetupSnapshot();
}

export function onFreeCaptureSetupRequest(
  listener: (snapshot: FreeCaptureSetupSnapshot) => void,
): () => void {
  freeCaptureSetupListeners.add(listener);
  if (freeCaptureSetupSnapshot.state === "pending") {
    listener(currentFreeCaptureSetupSnapshot());
  }
  return () => freeCaptureSetupListeners.delete(listener);
}

export function beginFreeCaptureSetup(requestId: number): boolean {
  if (
    freeCaptureSetupSnapshot.requestId !== requestId ||
    freeCaptureSetupSnapshot.state !== "pending"
  ) {
    return false;
  }
  freeCaptureSetupSnapshot = { ...freeCaptureSetupSnapshot, state: "running" };
  publishFreeCaptureSetup();
  return true;
}

export function finishFreeCaptureSetup(
  requestId: number,
  outcome: "succeeded" | "awaiting_selection" | "retryable_failed" | "failed",
  errorMessage: string | null = null,
): void {
  if (freeCaptureSetupSnapshot.requestId !== requestId) return;
  const retryable =
    outcome === "retryable_failed" &&
    freeCaptureSetupSnapshot.attempt + 1 < MAX_FREE_CAPTURE_SETUP_ATTEMPTS;
  freeCaptureSetupSnapshot = {
    ...freeCaptureSetupSnapshot,
    state: retryable ? "retryable_failed" : outcome === "retryable_failed" ? "failed" : outcome,
    errorMessage,
  };
  publishFreeCaptureSetup();
}

export function retryFreeCaptureSetup(requestId: number): boolean {
  if (
    freeCaptureSetupSnapshot.requestId !== requestId ||
    freeCaptureSetupSnapshot.state !== "retryable_failed"
  ) {
    return false;
  }
  freeCaptureSetupSnapshot = {
    ...freeCaptureSetupSnapshot,
    attempt: freeCaptureSetupSnapshot.attempt + 1,
    state: "pending",
    errorMessage: null,
  };
  publishFreeCaptureSetup();
  return true;
}

export function resetFreeCaptureSetupForTest(): void {
  nextFreeCaptureSetupRequestId = 1;
  freeCaptureSetupSnapshot = {
    requestId: null,
    reason: null,
    attempt: 0,
    state: "idle",
    errorMessage: null,
  };
  freeCaptureSetupListeners.clear();
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
