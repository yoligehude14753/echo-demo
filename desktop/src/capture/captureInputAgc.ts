export const CAPTURE_AGC_TARGET_RMS = 800 / 32_767;
export const CAPTURE_AGC_MAX_GAIN = 12;
export const CAPTURE_AGC_PEAK_HEADROOM = 0.92;
export const CAPTURE_AGC_FRAME_SAMPLES = 320; // 20 ms @ 16 kHz
export const CAPTURE_AGC_MIN_FRAME_ENERGY_CV = 0.75;

export interface CaptureAgcResult {
  samples: Float32Array;
  gainApplied: boolean;
  gain: number;
  clippedSamples: number;
}

/**
 * Speech changes energy across syllables; stationary microphone noise does not.
 * The coefficient is gain-invariant, so it must be checked before AGC destroys
 * the original level evidence by lifting every quiet chunk to the same target.
 */
export function captureFrameEnergyCv(samples: Float32Array): number {
  const frameCount = Math.floor(samples.length / CAPTURE_AGC_FRAME_SAMPLES);
  if (frameCount < 2) return 0;
  const frameRms: number[] = [];
  for (let frame = 0; frame < frameCount; frame += 1) {
    let squareSum = 0;
    const start = frame * CAPTURE_AGC_FRAME_SAMPLES;
    for (let index = start; index < start + CAPTURE_AGC_FRAME_SAMPLES; index += 1) {
      squareSum += samples[index] * samples[index];
    }
    frameRms.push(Math.sqrt(squareSum / CAPTURE_AGC_FRAME_SAMPLES));
  }
  const mean = frameRms.reduce((sum, value) => sum + value, 0) / frameRms.length;
  if (mean <= 0) return 0;
  const variance = frameRms.reduce(
    (sum, value) => sum + (value - mean) ** 2,
    0,
  ) / frameRms.length;
  return Math.sqrt(variance) / mean;
}

/** Apply bounded gain only to a real, already voiced PCM chunk. */
export function applyBoundedCaptureAgc(samples: Float32Array): CaptureAgcResult {
  let squareSum = 0;
  let peak = 0;
  for (const sample of samples) {
    squareSum += sample * sample;
    peak = Math.max(peak, Math.abs(sample));
  }
  const rms = samples.length > 0 ? Math.sqrt(squareSum / samples.length) : 0;
  if (rms <= 0 || rms >= CAPTURE_AGC_TARGET_RMS) {
    return { samples, gainApplied: false, gain: 1, clippedSamples: 0 };
  }
  // The old path amplified any low-energy chunk that the RMS-only client VAD
  // emitted. Ten seconds of stationary noise therefore landed at exactly RMS
  // 800 and crossed the backend admission threshold. Only speech-like energy
  // modulation may now authorize gain; unmodulated noise stays at its original
  // level and is rejected before ASR.
  if (captureFrameEnergyCv(samples) < CAPTURE_AGC_MIN_FRAME_ENERGY_CV) {
    return { samples, gainApplied: false, gain: 1, clippedSamples: 0 };
  }
  const targetGain = Math.min(CAPTURE_AGC_MAX_GAIN, CAPTURE_AGC_TARGET_RMS / rms);
  const headroomGain = peak > 0 ? CAPTURE_AGC_PEAK_HEADROOM / peak : 1;
  const gain = Math.max(1, Math.min(targetGain, headroomGain));
  if (gain <= 1) return { samples, gainApplied: false, gain: 1, clippedSamples: 0 };
  const amplified = new Float32Array(samples.length);
  let clippedSamples = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const raw = samples[index] * gain;
    const bounded = Math.max(-1, Math.min(1, raw));
    if (bounded !== raw) clippedSamples += 1;
    amplified[index] = bounded;
  }
  return { samples: amplified, gainApplied: true, gain, clippedSamples };
}
