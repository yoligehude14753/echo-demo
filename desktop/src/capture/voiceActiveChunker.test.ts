import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  VOICE_ACTIVITY_FRAME_SAMPLES,
  VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS,
  VOICE_ACTIVITY_MAX_CHUNK_MS,
  VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
  VOICE_ACTIVITY_POST_ROLL_FRAMES,
  VoiceActiveChunker,
} from "./voiceActiveChunker.ts";

function frame(amplitude: number): Float32Array {
  return new Float32Array(VOICE_ACTIVITY_FRAME_SAMPLES).fill(amplitude);
}

test("短句加静音在 post-roll 后发出有限大小的 voice-active 片段", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });

  for (let index = 0; index < 6; index += 1) chunker.push(frame(0));
  for (let index = 0; index < 5; index += 1) chunker.push(frame(0.04));
  for (let index = 0; index < VOICE_ACTIVITY_POST_ROLL_FRAMES - 1; index += 1) {
    chunker.push(frame(0));
  }
  assert.equal(emitted.length, 0, "必须保留完整 post-roll");

  chunker.push(frame(0));
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].length, 19 * VOICE_ACTIVITY_FRAME_SAMPLES);
  assert.ok(emitted[0].length / 16 <= VOICE_ACTIVITY_MAX_CHUNK_MS);
  assert.ok(VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS <= 1_000);
});

test("纯静音和不足两帧的瞬态都不会形成上传片段", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });

  for (let index = 0; index < 80; index += 1) chunker.push(frame(0));
  chunker.push(frame(0.04));
  for (let index = 0; index < VOICE_ACTIVITY_POST_ROLL_FRAMES; index += 1) {
    chunker.push(frame(0));
  }
  chunker.finish();

  assert.equal(emitted.length, 0);
});

test("连续有效语音按有限窗口无重叠且不丢样本", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  const source = new Float32Array(
    (VOICE_ACTIVITY_MAX_CHUNK_FRAMES * 2 + 7) * VOICE_ACTIVITY_FRAME_SAMPLES,
  );
  for (let index = 0; index < source.length; index += 1) {
    source[index] = 0.04 + index / source.length / 100;
  }

  chunker.push(source);
  chunker.finish();
  const merged = new Float32Array(emitted.reduce((sum, pcm) => sum + pcm.length, 0));
  let offset = 0;
  for (const pcm of emitted) {
    merged.set(pcm, offset);
    offset += pcm.length;
  }

  assert.deepEqual([...merged], [...source]);
  assert.ok(emitted.every((pcm) => pcm.length <= VOICE_ACTIVITY_MAX_CHUNK_FRAMES * VOICE_ACTIVITY_FRAME_SAMPLES));
});
