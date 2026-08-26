import assert from "node:assert/strict";
import test from "node:test";
import {
  applyBoundedCaptureAgc,
  CAPTURE_AGC_MAX_GAIN,
  CAPTURE_AGC_TARGET_RMS,
  captureFrameEnergyCv,
} from "./captureInputAgc.ts";

function quietSpeechLikeInput(): Float32Array {
  const samples = new Float32Array(320 * 8);
  const amplitudes = [0.0002, 0.004, 0.0004, 0.0035, 0.0003, 0.0045, 0.0002, 0.003];
  for (let frame = 0; frame < amplitudes.length; frame += 1) {
    samples.fill(amplitudes[frame], frame * 320, (frame + 1) * 320);
  }
  return samples;
}

test("low voiced input is lifted enough for backend RMS 800", () => {
  const input = quietSpeechLikeInput();
  const result = applyBoundedCaptureAgc(input);
  assert.equal(result.gainApplied, true);
  assert.ok(result.gain > 1 && result.gain <= CAPTURE_AGC_MAX_GAIN);
  const rms = Math.sqrt(
    result.samples.reduce((sum, sample) => sum + sample * sample, 0) /
      result.samples.length,
  );
  assert.ok(rms >= CAPTURE_AGC_TARGET_RMS * 0.99);
});
test("stationary low noise is never lifted to the backend admission floor", () => {
  const input = new Float32Array(320 * 8).fill(0.003);
  assert.ok(captureFrameEnergyCv(input) < 1e-12);
  const result = applyBoundedCaptureAgc(input);
  assert.equal(result.gainApplied, false);
  assert.equal(result.gain, 1);
  assert.deepEqual([...result.samples], [...input]);
});
test("zero/silence remains zero", () => {
  const result = applyBoundedCaptureAgc(new Float32Array(320));
  assert.deepEqual({ gainApplied: result.gainApplied, gain: result.gain, clippedSamples: result.clippedSamples }, { gainApplied: false, gain: 1, clippedSamples: 0 });
  assert.equal(result.samples[0], 0);
});
test("peak headroom prevents overflow", () => {
  const result = applyBoundedCaptureAgc(quietSpeechLikeInput());
  assert.ok(Math.max(...result.samples.map(Math.abs)) <= 0.92);
  assert.equal(result.clippedSamples, 0);
});
test("normal amplitude is unchanged", () => {
  const input = new Float32Array(320).fill(0.05);
  const result = applyBoundedCaptureAgc(input);
  assert.equal(result.gainApplied, false);
  assert.equal(result.gain, 1);
  assert.deepEqual([...result.samples], [...input]);
});
