/**
 * Capture segment correlation used by the WebView UI.
 *
 * The backend segment_id may contain the capture device identity. Keep the
 * raw value at the API boundary only and expose a session-scoped salted digest
 * to state and DOM so the same segment can be compared without disclosing the
 * identity or creating a cross-session identifier.
 */

const SESSION_SALT =
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

function hashSegment(value: string): string {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first ^= code;
    first = Math.imul(first, 0x01000193);
    second ^= code + index;
    second = Math.imul(second, 0x85ebca6b);
  }
  return `${(first >>> 0).toString(16).padStart(8, "0")}${
    (second >>> 0).toString(16).padStart(8, "0")
  }`;
}

export function captureSegmentCorrelationForSalt(
  segmentId: unknown,
  salt: string,
): string | null {
  if (typeof segmentId !== "string" || segmentId.trim() === "") return null;
  if (salt.trim() === "") return null;
  return `seg-${hashSegment(`${salt}\u0000${segmentId}`)}`;
}

export function captureSegmentCorrelation(segmentId: unknown): string | null {
  return captureSegmentCorrelationForSalt(segmentId, SESSION_SALT);
}

/** Ephemeral renderer-only salt shared with the native bridge in memory. */
export function captureCorrelationSessionSalt(): string {
  return SESSION_SALT;
}

export interface AmbientSegment {
  text: string;
  captured_at: string;
  speaker_id: string | null;
  speaker_label: string | null;
  duration_ms: number;
  /** Opaque, session-scoped value; never the backend segment_id. */
  segment_correlation: string | null;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function durationOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

export function normalizeAmbientSegments(value: unknown): AmbientSegment[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const body = record(item);
    return {
      text: stringOr(body.text, ""),
      captured_at: stringOr(body.captured_at, ""),
      speaker_id: nullableString(body.speaker_id),
      speaker_label: nullableString(body.speaker_label),
      duration_ms: durationOrZero(body.duration_ms),
      segment_correlation: captureSegmentCorrelation(body.segment_id),
    };
  });
}
