import type { CaptureState } from "@/domain/session";

export interface AudioCaptureSnapshot {
  state: CaptureState;
  errorMessage: string | null;
  lastErrorCode: string | null;
  revision: number;
}

export type AudioCaptureSnapshotHandler = (
  snapshot: AudioCaptureSnapshot,
) => void;

function captureErrorCode(errorMessage: string): string {
  const raw = errorMessage.toLowerCase();
  if (/permission|denied|notallowed/.test(raw)) return "permission";
  if (/device|notfound|microphone/.test(raw)) return "device";
  if (/timeout/.test(raw)) return "timeout";
  return "capture_error";
}

/**
 * The single lifecycle owner used by AudioCapture.
 *
 * A generation belongs to exactly one live capture attempt. Stopping or
 * invalidating an attempt advances the generation before publishing the next
 * state, so callbacks from an older MediaStream/native listener cannot revive
 * a stopped or replaced capture.
 */
export class AudioCaptureStateMachine {
  private generation = 0;
  private running = false;
  private snapshot: AudioCaptureSnapshot = Object.freeze({
    state: "standby",
    errorMessage: null,
    lastErrorCode: null,
    revision: 0,
  });
  private handlers = new Set<AudioCaptureSnapshotHandler>();

  getSnapshot(): AudioCaptureSnapshot {
    return this.snapshot;
  }

  getCurrentGeneration(): number | null {
    return this.running ? this.generation : null;
  }

  begin(): number | null {
    if (this.running) return null;
    this.running = true;
    this.generation += 1;
    this.publish("initializing");
    return this.generation;
  }

  beginRetry(generation: number): boolean {
    if (!this.isCurrent(generation)) return false;
    this.publish("initializing");
    return true;
  }

  markCapturing(generation: number): boolean {
    if (!this.isCurrent(generation)) return false;
    this.publish("capturing");
    return true;
  }

  /**
   * Fail the current asynchronous attempt and return the generation reserved
   * for its retry. The failed attempt becomes stale immediately.
   */
  invalidateWithError(
    generation: number,
    errorMessage: string,
  ): number | null {
    if (!this.isCurrent(generation)) return null;
    this.generation += 1;
    this.publish("error", errorMessage);
    return this.generation;
  }

  stop(): void {
    this.running = false;
    this.generation += 1;
    this.publish("standby");
  }

  isCurrent(generation: number): boolean {
    return this.running && this.generation === generation;
  }

  subscribe(handler: AudioCaptureSnapshotHandler): () => void {
    this.handlers.add(handler);
    handler(this.snapshot);
    return () => this.handlers.delete(handler);
  }

  private publish(state: CaptureState, errorMessage?: string): void {
    const normalizedError = state === "error" ? errorMessage ?? "" : null;
    const lastErrorCode =
      state === "error" ? captureErrorCode(normalizedError ?? "") : null;
    if (
      this.snapshot.state === state &&
      this.snapshot.errorMessage === normalizedError &&
      this.snapshot.lastErrorCode === lastErrorCode
    ) {
      return;
    }
    this.snapshot = Object.freeze({
      state,
      errorMessage: normalizedError,
      lastErrorCode,
      revision: this.snapshot.revision + 1,
    });
    for (const handler of this.handlers) handler(this.snapshot);
  }
}
