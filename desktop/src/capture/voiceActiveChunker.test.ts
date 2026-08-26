import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import {
  VOICE_ACTIVITY_FRAME_SAMPLES,
  VOICE_ACTIVITY_FRAME_RMS,
  VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS,
  VOICE_ACTIVITY_MAX_CHUNK_MS,
  VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
  VOICE_ACTIVITY_MIN_FRAMES,
  VOICE_ACTIVITY_POST_ROLL_FRAMES,
  VOICE_ACTIVITY_PRE_ROLL_FRAMES,
  resolveVoiceActivityMaxChunkFrames,
  resolveVoiceActivityPostRollFrames,
  VoiceActiveChunker,
} from "./voiceActiveChunker.ts";
// @ts-expect-error Node strip-types requires the explicit source extension.
import { applyBoundedCaptureAgc, CAPTURE_AGC_MAX_GAIN } from "./captureInputAgc.ts";
// @ts-expect-error Node strip-types requires the explicit source extension.
import { floatTo16BitPCM, pcm16ToWav } from "./pcm.ts";
// @ts-expect-error Node strip-types requires the explicit source extension.
import { CAPTURE_SPOOL_MAX_BYTES } from "./captureUploadSpool.ts";

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
  assert.equal(emitted[0].length, 71 * VOICE_ACTIVITY_FRAME_SAMPLES);
  assert.ok(emitted[0].length / 16 <= VOICE_ACTIVITY_MAX_CHUNK_MS);
  assert.equal(VOICE_ACTIVITY_MAX_CHUNK_MS, 15_000);
  assert.equal(VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS, 15_256);
});

test("采集自然结束会立即 flush 已验证短语尾而不等待连续语音窗口", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  for (let index = 0; index < 3; index += 1) chunker.push(frame(0.04));

  chunker.finish();

  assert.deepEqual(
    emitted.map((pcm) => pcm.length / VOICE_ACTIVITY_FRAME_SAMPLES),
    [3],
  );
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

test("低幅度但非零的真实输入仍可通过 VAD，纯静音继续拒绝", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  for (let index = 0; index < 4; index += 1) chunker.push(frame(0.003));
  for (let index = 0; index < VOICE_ACTIVITY_POST_ROLL_FRAMES; index += 1) {
    chunker.push(frame(0));
  }
  assert.equal(emitted.length, 1);
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
  assert.equal(VOICE_ACTIVITY_MAX_CHUNK_FRAMES, 750);
  assert.deepEqual(
    emitted.map((pcm) => pcm.length / VOICE_ACTIVITY_FRAME_SAMPLES),
    [VOICE_ACTIVITY_MAX_CHUNK_FRAMES, VOICE_ACTIVITY_MAX_CHUNK_FRAMES, 7],
  );
  assert.ok(emitted.every((pcm) => pcm.length <= VOICE_ACTIVITY_MAX_CHUNK_FRAMES * VOICE_ACTIVITY_FRAME_SAMPLES));
});

test("15 秒最大 voice-active WAV 约 469KiB 且远低于 durable 1GiB 上限", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  for (let index = 0; index < VOICE_ACTIVITY_MAX_CHUNK_FRAMES; index += 1) {
    chunker.push(frame(0.04));
  }

  assert.equal(emitted.length, 1);
  const wav = pcm16ToWav(floatTo16BitPCM(emitted[0]), 16_000);
  assert.equal(wav.size, 480_044);
  assert.ok(wav.size * 800 < CAPTURE_SPOOL_MAX_BYTES);
});

test("cadence 调整不改变 VAD 门限、roll 语义或语音起伏后有界 AGC", () => {
  assert.equal(VOICE_ACTIVITY_FRAME_RMS, 64 / 32_767);
  assert.equal(VOICE_ACTIVITY_MIN_FRAMES, 2);
  assert.equal(VOICE_ACTIVITY_PRE_ROLL_FRAMES, 6);
  assert.equal(VOICE_ACTIVITY_POST_ROLL_FRAMES, 60);

  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  const speechEnvelope = [0.0002, 0.004, 0.0004, 0.0035, 0.0003, 0.0045];
  for (let index = 0; index < VOICE_ACTIVITY_MAX_CHUNK_FRAMES; index += 1) {
    chunker.push(frame(speechEnvelope[index % speechEnvelope.length]));
  }
  assert.equal(emitted.length, 1);
  const agc = applyBoundedCaptureAgc(emitted[0]);
  assert.equal(agc.samples.length, emitted[0].length);
  assert.equal(agc.gainApplied, true);
  assert.ok(agc.gain > 1 && agc.gain <= CAPTURE_AGC_MAX_GAIN);
});

test("连续语音窗口可配置，仍不重复样本或上传静音", () => {
  const emitted: Float32Array[] = [];
  const maxChunkFrames = 16;
  const chunker = new VoiceActiveChunker({
    emit: (pcm) => emitted.push(pcm),
    maxChunkFrames,
  });
  const source = new Float32Array(
    (maxChunkFrames * 2 + 5) * VOICE_ACTIVITY_FRAME_SAMPLES,
  ).fill(0.04);

  chunker.push(source);
  chunker.finish();

  assert.deepEqual(
    emitted.map((pcm) => pcm.length / VOICE_ACTIVITY_FRAME_SAMPLES),
    [maxChunkFrames, maxChunkFrames, 5],
  );
  assert.deepEqual(
    [...emitted.flatMap((pcm) => [...pcm])],
    [...source],
  );
});

test("构建时 chunk 时长覆盖有界，非法值回退到默认 15 秒", () => {
  assert.equal(resolveVoiceActivityMaxChunkFrames("12000"), 600);
  assert.equal(resolveVoiceActivityMaxChunkFrames("15000"), 750);
  assert.equal(resolveVoiceActivityMaxChunkFrames("500"), VOICE_ACTIVITY_MAX_CHUNK_FRAMES);
  assert.equal(resolveVoiceActivityMaxChunkFrames("16000"), VOICE_ACTIVITY_MAX_CHUNK_FRAMES);
  assert.equal(resolveVoiceActivityMaxChunkFrames("not-a-number"), VOICE_ACTIVITY_MAX_CHUNK_FRAMES);
});

test("构建时 post-roll 覆盖有界，默认 1.2 秒且实例选项真正生效", () => {
  assert.equal(resolveVoiceActivityPostRollFrames("1200"), 60);
  assert.equal(resolveVoiceActivityPostRollFrames("200"), 10);
  assert.equal(resolveVoiceActivityPostRollFrames("2000"), 100);
  assert.equal(resolveVoiceActivityPostRollFrames("100"), VOICE_ACTIVITY_POST_ROLL_FRAMES);
  assert.equal(resolveVoiceActivityPostRollFrames("2200"), VOICE_ACTIVITY_POST_ROLL_FRAMES);

  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({
    emit: (pcm) => emitted.push(pcm),
    postRollFrames: 10,
  });
  for (let index = 0; index < 3; index += 1) chunker.push(frame(0.04));
  for (let index = 0; index < 9; index += 1) chunker.push(frame(0));
  assert.equal(emitted.length, 0);
  chunker.push(frame(0));
  assert.equal(emitted.length, 1);
});

test("连续 60 秒语音按 15 秒窗口最多产生 4 个整块和 1 个尾段", () => {
  const emitted: Float32Array[] = [];
  const chunker = new VoiceActiveChunker({ emit: (pcm) => emitted.push(pcm) });
  const source = new Float32Array(
    (VOICE_ACTIVITY_MAX_CHUNK_FRAMES * 4 + 7) * VOICE_ACTIVITY_FRAME_SAMPLES,
  ).fill(0.04);
  chunker.push(source);
  chunker.finish();
  assert.deepEqual(
    emitted.map((pcm) => pcm.length / VOICE_ACTIVITY_FRAME_SAMPLES),
    [
      VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
      VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
      VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
      VOICE_ACTIVITY_MAX_CHUNK_FRAMES,
      7,
    ],
  );
  assert.deepEqual(
    [...emitted.flatMap((pcm) => [...pcm])],
    [...source],
  );
});

test("非法连续语音窗口配置回退到默认 15 秒上限", () => {
  const emitted: Float32Array[] = [];
  const source = new Float32Array(
    (VOICE_ACTIVITY_MAX_CHUNK_FRAMES + 2) * VOICE_ACTIVITY_FRAME_SAMPLES,
  ).fill(0.04);
  const chunker = new VoiceActiveChunker({
    emit: (pcm) => emitted.push(pcm),
    maxChunkFrames: 1,
  });

  chunker.push(source);
  chunker.finish();

  assert.deepEqual(
    emitted.map((pcm) => pcm.length / VOICE_ACTIVITY_FRAME_SAMPLES),
    [VOICE_ACTIVITY_MAX_CHUNK_FRAMES, 2],
  );
});
