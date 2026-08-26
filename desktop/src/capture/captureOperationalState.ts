import type { CaptureStats } from "@/api";
import type { CaptureStatus as CaptureStatusModel } from "@/domain/session";

// User-visible pending capacity reflects the durable upload spool, while the
// renderer's serialized enqueue tail is diagnostic-only and never a drop gate.
export const CAPTURE_QUEUE_CAPACITY = 8_192;
export const CAPTURE_STATS_FAILURE_THRESHOLD = 2;
export const CAPTURE_ADMISSION_RECENT_MS = 15_000;

export type CaptureGateReason =
  | "ok"
  | "rms_too_low"
  | "speech_ratio_too_low"
  | "stationary_noise"
  | "unknown";

export type CaptureTransportWarning =
  | "none"
  | "upload_unavailable"
  | "backpressure";

export type CaptureFreshnessWarning = "none" | "stats_unavailable";

export type CaptureAdmissionWarning =
  | "none"
  | "rms_too_low"
  | "speech_ratio_too_low"
  | "stationary_noise";

export interface CaptureTransportState {
  queueDepth: number;
  queueCapacity: number;
  recovering: boolean;
  inFlight: boolean;
  sent: number;
  acknowledged: number;
  droppedBackpressure: number;
  consecutiveFailures: number;
  lastSuccessfulUploadAt: number | null;
  warning: CaptureTransportWarning;
}

export interface CaptureFreshnessState {
  warning: CaptureFreshnessWarning;
  consecutiveFailures: number;
  source: "sequence" | "timestamp" | "legacy";
  lastSequence: number | null;
  lastTimestamp: string | null;
  lastFreshAt: number | null;
}

export interface CaptureAdmissionState {
  warning: CaptureAdmissionWarning;
  lastGateReason: CaptureGateReason | null;
  lastRms: number | null;
  lastSpeechRatio: number | null;
  acceptedSpeechFrames: number | null;
  observedAudioFrames: number | null;
  acceptedSpeechRatio: number | null;
  lastObservedAt: number | null;
}

export interface CaptureOperationalState {
  transport: CaptureTransportState;
  freshness: CaptureFreshnessState;
  admission: CaptureAdmissionState;
}

export type CaptureViewModel = CaptureStatusModel & CaptureOperationalState;

export function createCaptureTransportState(
  queueCapacity = CAPTURE_QUEUE_CAPACITY,
): CaptureTransportState {
  return {
    queueDepth: 0,
    queueCapacity,
    recovering: false,
    inFlight: false,
    sent: 0,
    acknowledged: 0,
    droppedBackpressure: 0,
    consecutiveFailures: 0,
    lastSuccessfulUploadAt: null,
    warning: "none",
  };
}

/**
 * A durable backend receipt is authoritative proof that the upload transport
 * is currently usable.  It must recover a warning even when the receipt came
 * from a partition that became a recovery lane after a formal meeting ended.
 * Attempt/acknowledgement counters are owned by CaptureUploadPool and are left
 * untouched here.
 */
export function observeDurableCaptureAcknowledgement(
  current: CaptureTransportState,
  options: { now?: number; backpressureActive?: boolean } = {},
): CaptureTransportState {
  return {
    ...current,
    inFlight: false,
    consecutiveFailures: 0,
    lastSuccessfulUploadAt: options.now ?? Date.now(),
    warning: options.backpressureActive ? "backpressure" : "none",
  };
}

export function createCaptureFreshnessState(): CaptureFreshnessState {
  return {
    warning: "none",
    consecutiveFailures: 0,
    source: "legacy",
    lastSequence: null,
    lastTimestamp: null,
    lastFreshAt: null,
  };
}

export function createCaptureAdmissionState(): CaptureAdmissionState {
  return {
    warning: "none",
    lastGateReason: null,
    lastRms: null,
    lastSpeechRatio: null,
    acceptedSpeechFrames: null,
    observedAudioFrames: null,
    acceptedSpeechRatio: null,
    lastObservedAt: null,
  };
}

export function normalizeCaptureGateReason(
  reason: unknown,
): CaptureGateReason | null {
  if (reason === null || reason === undefined || reason === "") return null;
  if (
    reason === "ok" ||
    reason === "rms_too_low" ||
    reason === "speech_ratio_too_low" ||
    reason === "stationary_noise"
  ) {
    return reason;
  }
  return "unknown";
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isNewerTimestamp(next: string, previous: string | null): boolean {
  if (!previous) return true;
  const nextMs = Date.parse(next);
  const previousMs = Date.parse(previous);
  if (Number.isFinite(nextMs) && Number.isFinite(previousMs)) {
    return nextMs > previousMs;
  }
  return next !== previous;
}

export function observeCaptureStatsSuccess(
  current: CaptureFreshnessState,
  stats: CaptureStats,
  now = Date.now(),
): CaptureFreshnessState {
  const sequence = finiteNumber(stats.stats_sequence);
  const timestamp =
    typeof stats.last_chunk_at === "string" && stats.last_chunk_at.length > 0
      ? stats.last_chunk_at
      : null;
  const sequenceAdvanced =
    sequence !== null &&
    (current.lastSequence === null || sequence > current.lastSequence);
  const timestampAdvanced =
    timestamp !== null && isNewerTimestamp(timestamp, current.lastTimestamp);
  const generationReset =
    sequence !== null &&
    current.lastSequence !== null &&
    sequence < current.lastSequence &&
    timestampAdvanced;
  const advanced = sequenceAdvanced || timestampAdvanced;
  const source =
    timestampAdvanced && !sequenceAdvanced
      ? "timestamp"
      : sequence !== null
        ? "sequence"
        : timestamp
          ? "timestamp"
          : "legacy";

  return {
    warning: advanced ? "none" : current.warning,
    consecutiveFailures: 0,
    source,
    lastSequence:
      sequence !== null &&
      (current.lastSequence === null || sequenceAdvanced || generationReset)
        ? sequence
        : current.lastSequence,
    lastTimestamp:
      timestamp !== null &&
      (current.lastTimestamp === null || timestampAdvanced)
        ? timestamp
        : current.lastTimestamp,
    lastFreshAt: advanced ? now : current.lastFreshAt,
  };
}

export function observeCaptureStatsFailure(
  current: CaptureFreshnessState,
): CaptureFreshnessState {
  const consecutiveFailures = current.consecutiveFailures + 1;
  return {
    ...current,
    consecutiveFailures,
    warning:
      consecutiveFailures >= CAPTURE_STATS_FAILURE_THRESHOLD
        ? "stats_unavailable"
        : current.warning,
  };
}

export function observeCaptureAdmission(
  current: CaptureAdmissionState,
  previous: CaptureStats | null,
  next: CaptureStats,
  now = Date.now(),
): CaptureAdmissionState {
  const previousChunks = finiteNumber(previous?.chunks_total) ?? 0;
  const nextChunks = finiteNumber(next.chunks_total) ?? 0;
  const previousStored = finiteNumber(previous?.stored) ?? 0;
  const nextStored = finiteNumber(next.stored) ?? 0;
  const previousGatedRms = finiteNumber(previous?.gated_rms) ?? 0;
  const nextGatedRms = finiteNumber(next.gated_rms) ?? 0;
  const previousGatedSpeech = finiteNumber(previous?.gated_low_speech) ?? 0;
  const nextGatedSpeech = finiteNumber(next.gated_low_speech) ?? 0;
  const previousStationaryNoise =
    finiteNumber(previous?.gated_stationary_noise) ?? 0;
  const nextStationaryNoise = finiteNumber(next.gated_stationary_noise) ?? 0;
  const previousAccepted = finiteNumber(previous?.accepted_speech_frames);
  const nextAccepted = finiteNumber(next.accepted_speech_frames);
  const previousObserved = finiteNumber(previous?.observed_audio_frames);
  const nextObserved = finiteNumber(next.observed_audio_frames);
  const previousSequence = finiteNumber(previous?.stats_sequence);
  const nextSequence = finiteNumber(next.stats_sequence);
  const previousTimestamp =
    typeof previous?.last_chunk_at === "string" && previous.last_chunk_at.length > 0
      ? previous.last_chunk_at
      : null;
  const nextTimestamp =
    typeof next.last_chunk_at === "string" && next.last_chunk_at.length > 0
      ? next.last_chunk_at
      : null;
  const gateReason = normalizeCaptureGateReason(next.last_gate_reason);
  // stats_sequence 是 backend 进程生命周期内的游标。回退表示 backend
  // 重启，重启后的低位计数不能再和旧进程的累计值直接比较。
  const backendGenerationReset =
    previousSequence !== null &&
    nextSequence !== null &&
    nextSequence < previousSequence &&
    nextTimestamp !== null &&
    isNewerTimestamp(nextTimestamp, previousTimestamp);
  const newChunk = nextChunks > previousChunks;
  const newAcceptedSpeech =
    nextAccepted !== null &&
    (previousAccepted === null || nextAccepted > previousAccepted);
  const newObservedAudio =
    nextObserved !== null &&
    (previousObserved === null || nextObserved > previousObserved);
  const newAdmissionObservation =
    backendGenerationReset ||
    newChunk ||
    newAcceptedSpeech ||
    newObservedAudio ||
    nextStored > previousStored;
  const newLowRms =
    newAdmissionObservation &&
    (gateReason === "rms_too_low" || nextGatedRms > previousGatedRms);
  const newLowSpeech =
    newAdmissionObservation &&
    (gateReason === "speech_ratio_too_low" ||
      nextGatedSpeech > previousGatedSpeech);
  const newStationaryNoise =
    newAdmissionObservation &&
    (gateReason === "stationary_noise" ||
      nextStationaryNoise > previousStationaryNoise);
  const validSpeechObservation =
    newAdmissionObservation &&
    (gateReason === "ok" || newAcceptedSpeech || nextStored > previousStored);

  let warning = current.warning;
  if (newLowRms) warning = "rms_too_low";
  else if (newLowSpeech) warning = "speech_ratio_too_low";
  else if (newStationaryNoise) warning = "stationary_noise";
  else if (validSpeechObservation) warning = "none";

  return {
    warning,
    lastGateReason: newAdmissionObservation
      ? gateReason
      : current.lastGateReason,
    lastRms: finiteNumber(next.last_rms),
    lastSpeechRatio: finiteNumber(next.last_speech_ratio),
    acceptedSpeechFrames: nextAccepted,
    observedAudioFrames: nextObserved,
    acceptedSpeechRatio: finiteNumber(next.accepted_speech_ratio),
    lastObservedAt: newAdmissionObservation ? now : current.lastObservedAt,
  };
}

export function hasRecentAcceptedSpeech(
  state: CaptureAdmissionState,
  now = Date.now(),
): boolean {
  return (
    state.lastGateReason === "ok" &&
    state.lastObservedAt !== null &&
    now - state.lastObservedAt <= CAPTURE_ADMISSION_RECENT_MS
  );
}
