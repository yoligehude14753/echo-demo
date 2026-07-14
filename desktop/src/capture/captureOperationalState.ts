import type { CaptureStats } from "@/api";
import type { CaptureStatus as CaptureStatusModel } from "@/domain/session";

export const CAPTURE_QUEUE_CAPACITY = 4;
export const CAPTURE_STATS_FAILURE_THRESHOLD = 2;

export type CaptureGateReason =
  | "ok"
  | "rms_too_low"
  | "speech_ratio_too_low"
  | "unknown";

export type CaptureTransportWarning =
  | "none"
  | "upload_unavailable"
  | "backpressure";

export type CaptureFreshnessWarning = "none" | "stats_unavailable";

export type CaptureAdmissionWarning =
  | "none"
  | "rms_too_low"
  | "speech_ratio_too_low";

export interface CaptureTransportState {
  queueDepth: number;
  queueCapacity: number;
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
    inFlight: false,
    sent: 0,
    acknowledged: 0,
    droppedBackpressure: 0,
    consecutiveFailures: 0,
    lastSuccessfulUploadAt: null,
    warning: "none",
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
  };
}

export function normalizeCaptureGateReason(
  reason: unknown,
): CaptureGateReason | null {
  if (reason === null || reason === undefined || reason === "") return null;
  if (
    reason === "ok" ||
    reason === "rms_too_low" ||
    reason === "speech_ratio_too_low"
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
  const source = sequence !== null ? "sequence" : timestamp ? "timestamp" : "legacy";
  const advanced =
    sequence !== null
      ? current.lastSequence === null || sequence > current.lastSequence
      : timestamp !== null && isNewerTimestamp(timestamp, current.lastTimestamp);

  return {
    warning: advanced ? "none" : current.warning,
    consecutiveFailures: 0,
    source,
    lastSequence: sequence ?? current.lastSequence,
    lastTimestamp: timestamp ?? current.lastTimestamp,
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
): CaptureAdmissionState {
  const previousChunks = finiteNumber(previous?.chunks_total) ?? 0;
  const nextChunks = finiteNumber(next.chunks_total) ?? 0;
  const previousStored = finiteNumber(previous?.stored) ?? 0;
  const nextStored = finiteNumber(next.stored) ?? 0;
  const previousGatedRms = finiteNumber(previous?.gated_rms) ?? 0;
  const nextGatedRms = finiteNumber(next.gated_rms) ?? 0;
  const previousGatedSpeech = finiteNumber(previous?.gated_low_speech) ?? 0;
  const nextGatedSpeech = finiteNumber(next.gated_low_speech) ?? 0;
  const previousAccepted = finiteNumber(previous?.accepted_speech_frames);
  const nextAccepted = finiteNumber(next.accepted_speech_frames);
  const previousObserved = finiteNumber(previous?.observed_audio_frames);
  const nextObserved = finiteNumber(next.observed_audio_frames);
  const gateReason = normalizeCaptureGateReason(next.last_gate_reason);
  const newChunk = nextChunks > previousChunks;
  const newAcceptedSpeech =
    nextAccepted !== null &&
    (previousAccepted === null || nextAccepted > previousAccepted);
  const newObservedAudio =
    nextObserved !== null &&
    (previousObserved === null || nextObserved > previousObserved);
  const newAdmissionObservation =
    newChunk || newAcceptedSpeech || newObservedAudio || nextStored > previousStored;
  const newLowRms =
    newAdmissionObservation &&
    (gateReason === "rms_too_low" || nextGatedRms > previousGatedRms);
  const newLowSpeech =
    newAdmissionObservation &&
    (gateReason === "speech_ratio_too_low" ||
      nextGatedSpeech > previousGatedSpeech);
  const validSpeechObservation =
    newAdmissionObservation &&
    (gateReason === "ok" || newAcceptedSpeech || nextStored > previousStored);

  let warning = current.warning;
  if (newLowRms) warning = "rms_too_low";
  else if (newLowSpeech) warning = "speech_ratio_too_low";
  else if (validSpeechObservation) warning = "none";

  return {
    warning,
    lastGateReason: gateReason,
    lastRms: finiteNumber(next.last_rms),
    lastSpeechRatio: finiteNumber(next.last_speech_ratio),
    acceptedSpeechFrames: nextAccepted,
    observedAudioFrames: nextObserved,
    acceptedSpeechRatio: finiteNumber(next.accepted_speech_ratio),
  };
}
