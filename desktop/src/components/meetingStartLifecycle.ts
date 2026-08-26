/**
 * Formal meeting lifecycle is intentionally independent from microphone
 * readiness. The meeting boundary is durable first; capture only attaches to
 * a still-current boundary afterwards.
 */
export interface FormalMeetingSnapshot {
  mode: "idle" | "in_meeting";
  meeting_id: string | null;
  started_at: string | null;
  started_by: "manual" | "auto" | null;
}

export const REQUEST_FORMAL_MEETING_EVENT = "echodesk:request-formal-meeting";
/**
 * STT admission is remote and a short meeting can leave a bounded durable
 * tail behind. Keep the product timeout finite, but long enough to drain the
 * normal formal tail before refusing to close the backend append gate.
 */
export const DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS = 240_000;

/** 供纪要空态等非顶栏入口请求走同一套正式会议生命周期。 */
export function requestFormalMeeting(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(REQUEST_FORMAL_MEETING_EVENT));
}

export interface StartFormalMeetingDependencies {
  manualStart: () => Promise<FormalMeetingSnapshot>;
  isCurrent: () => boolean;
  nextCaptureGeneration: () => number;
  activate: (snapshot: FormalMeetingSnapshot) => void;
  prepareCapture: (generation: number) => Promise<void>;
  onCaptureError: (error: unknown) => void;
}

/**
 * Commits the meeting before launching capture work. A capture rejection is
 * deliberately contained and cannot roll back the committed meeting.
 */
export async function startFormalMeetingLifecycle(
  dependencies: StartFormalMeetingDependencies,
): Promise<boolean> {
  const generation = dependencies.nextCaptureGeneration();
  const snapshot = await dependencies.manualStart();
  if (!dependencies.isCurrent()) return false;
  dependencies.activate(snapshot);
  void dependencies.prepareCapture(generation).catch(dependencies.onCaptureError);
  return true;
}

export interface EndFormalMeetingDependencies {
  manualEnd: () => Promise<FormalMeetingSnapshot>;
  isCurrent: () => boolean;
  stopCaptureProducer: () => void;
  awaitCaptureRouterDrain: () => Promise<void>;
  awaitFormalPartitionIdle: () => Promise<void>;
  restoreCapture: () => void;
  /** Read the authoritative meeting fence before restoring after a timeout. */
  canRestoreCapture: () => Promise<boolean>;
  /** Terminal watchdog/lifecycle won; clear the renderer fence without manualEnd. */
  onTerminalCapture?: () => void;
  invalidateCapture: () => void;
  deactivate: (snapshot: FormalMeetingSnapshot) => void;
  releaseFormalCapture?: () => void;
  onStopError?: (error: unknown) => void;
  quiesceTimeoutMs?: number;
}

async function restoreCaptureIfStillActive(
  dependencies: EndFormalMeetingDependencies,
  timeoutMs: number,
): Promise<"restored" | "terminal" | "unknown"> {
  try {
    const canRestore = await withTimeout(
      dependencies.canRestoreCapture(),
      Math.min(timeoutMs, 5_000),
    );
    if (!canRestore) {
      dependencies.onTerminalCapture?.();
      return "terminal";
    }
  } catch {
    // Failing closed is required: producer recovery needs a fresh authoritative
    // active-meeting confirmation, not a stale renderer snapshot.
    return "unknown";
  }
  dependencies.restoreCapture();
  return "restored";
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("formal capture quiesce timed out")),
      timeoutMs,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/** Stop producer and drain the formal partition before the end API is awaited. */
export async function endFormalMeetingLifecycle(
  dependencies: EndFormalMeetingDependencies,
): Promise<boolean> {
  const timeoutMs =
    dependencies.quiesceTimeoutMs ?? DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS;
  dependencies.stopCaptureProducer();
  dependencies.invalidateCapture();
  try {
    await withTimeout(dependencies.awaitCaptureRouterDrain(), timeoutMs);
    await withTimeout(dependencies.awaitFormalPartitionIdle(), timeoutMs);
  } catch (error) {
    const recovery = await restoreCaptureIfStillActive(dependencies, timeoutMs);
    if (recovery !== "terminal") dependencies.onStopError?.(error);
    return false;
  }
  let snapshot: FormalMeetingSnapshot;
  try {
    snapshot = await dependencies.manualEnd();
  } catch (error) {
    const recovery = await restoreCaptureIfStillActive(dependencies, timeoutMs);
    if (recovery !== "terminal") dependencies.onStopError?.(error);
    return false;
  }
  if (!dependencies.isCurrent()) return false;
  dependencies.deactivate(snapshot);
  dependencies.releaseFormalCapture?.();
  dependencies.restoreCapture();
  return true;
}

export function isFormalCaptureAttachmentCurrent(
  currentGeneration: number,
  requestedGeneration: number,
  formalMeetingId: string | null,
): boolean {
  return currentGeneration === requestedGeneration && formalMeetingId !== null;
}
