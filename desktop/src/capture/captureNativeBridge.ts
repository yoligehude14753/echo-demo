export interface NativeCaptureUploadResult {
  segment_correlation: string;
  ambient_stored: boolean;
  ambient_text: string | null;
}

const OPAQUE_CORRELATION = /^seg-[0-9a-f]{16}$/;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * Accept only the allowlisted native success payload. Raw identifiers and
 * diagnostic hashes are intentionally ignored before state is touched.
 */
export function normalizeNativeCaptureUpload(
  value: unknown,
): NativeCaptureUploadResult | null {
  const body = record(value);
  const correlation = body.segmentCorrelation;
  if (typeof correlation !== "string" || !OPAQUE_CORRELATION.test(correlation)) {
    return null;
  }
  const stored = body.ambientStored === true;
  return {
    segment_correlation: correlation,
    ambient_stored: stored,
    ambient_text:
      stored && typeof body.ambientText === "string" ? body.ambientText : null,
  };
}
