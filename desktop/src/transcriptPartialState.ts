import type { EchoEvent } from "@/types";

export type TranscriptPartialState = "partial" | "completed";

export interface TranscriptPartialProjection {
  correlation: string;
  text: string;
  state: TranscriptPartialState;
  meetingId: string | null;
  capturedAt: string;
}

export type TranscriptPartialMap = Record<string, TranscriptPartialProjection>;

const CAPTURE_CORRELATION = /^capture-[0-9a-f]{16}$/;
const MAX_PARTIAL_CHARS = 32_000;

export function clearTranscriptPartial(
  current: TranscriptPartialMap,
  correlation: string | null | undefined,
): TranscriptPartialMap {
  if (!correlation || current[correlation] === undefined) return current;
  const next = { ...current };
  delete next[correlation];
  return next;
}

export function applyTranscriptPartialEvent(
  current: TranscriptPartialMap,
  event: EchoEvent,
): TranscriptPartialMap {
  if (event.type !== "transcript.partial") return current;
  const payload = event.payload as Record<string, unknown>;
  const correlation = payload.correlation;
  const text = payload.text;
  const state = payload.state;
  if (
    typeof correlation !== "string" ||
    !CAPTURE_CORRELATION.test(correlation) ||
    typeof text !== "string" ||
    text.length > MAX_PARTIAL_CHARS ||
    !["partial", "completed", "failed"].includes(String(state))
  ) {
    return current;
  }
  if (state === "failed" || (state === "completed" && text.length === 0)) {
    return clearTranscriptPartial(current, correlation);
  }
  const previous = current[correlation];
  return {
    ...current,
    [correlation]: {
      correlation,
      text,
      state: state as TranscriptPartialState,
      meetingId: event.meeting_id ?? previous?.meetingId ?? null,
      capturedAt: previous?.capturedAt ?? event.ts,
    },
  };
}

export function visibleTranscriptPartials(
  current: TranscriptPartialMap,
  meetingId: string | null,
): TranscriptPartialProjection[] {
  return Object.values(current)
    .filter((partial) => partial.meetingId === meetingId)
    .sort((left, right) => left.capturedAt.localeCompare(right.capturedAt));
}
