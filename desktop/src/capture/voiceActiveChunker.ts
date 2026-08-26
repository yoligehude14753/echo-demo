/**
 * 自由收音的本地语音片段器。
 *
 * 仅在含有足够有效语音的片段完成时回调；静音不会产生上传载荷。这里输出的
 * 仍是原始 16 kHz PCM，WAV 封装和 HTTP 合约继续由既有 Capture 层处理。
 */

export const VOICE_ACTIVITY_SAMPLE_RATE = 16_000;
export const VOICE_ACTIVITY_FRAME_SAMPLES = 320; // 20 ms
// macOS aggregate microphone input can be normalized below 0.01 even for a
// live physical microphone. 800/32767 rejected the observed ~0.0028 RMS
// signal before a chunk could ever be produced. Keep a floor above the
// all-zero path while accepting quiet real speech.
export const VOICE_ACTIVITY_FRAME_RMS = 64 / 32_767;
export const VOICE_ACTIVITY_MIN_FRAMES = 2;
export const VOICE_ACTIVITY_PRE_ROLL_FRAMES = 6; // 120 ms
export const VOICE_ACTIVITY_POST_ROLL_FRAMES = 60; // 1,200 ms
// 桌面 WebAudio 默认以 15 秒连续语音窗口上传。远端 ASR 服务的吞吐低于
// 1 秒碎片的生产速率；更长的自然语音窗口同时降低 durable spool 压力并保留
// 句子语义。构建环境可在安全范围内覆盖，不绑定任何模型。
export const VOICE_ACTIVITY_MAX_CHUNK_FRAMES = 750; // 15,000 ms

export const VOICE_ACTIVITY_MAX_CHUNK_MS =
  (VOICE_ACTIVITY_MAX_CHUNK_FRAMES * VOICE_ACTIVITY_FRAME_SAMPLES * 1_000) /
  VOICE_ACTIVITY_SAMPLE_RATE;
// 保守按 ScriptProcessor 在 16 kHz 下最多约 256 ms 的调度余量计算；默认
// 连续语音窗口是 15 s，短语自然停顿则需达到 1.2 s post-roll 后才 emit。
export const VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS =
  VOICE_ACTIVITY_MAX_CHUNK_MS + 256;

export const VOICE_ACTIVITY_CHUNK_MS_ENV = "VITE_ECHODESK_CAPTURE_CHUNK_MS";
export const VOICE_ACTIVITY_MIN_CONFIGURED_CHUNK_MS = 1_000;
export const VOICE_ACTIVITY_MAX_CONFIGURED_CHUNK_MS = 15_000;
export const VOICE_ACTIVITY_POST_ROLL_MS_ENV =
  "VITE_ECHODESK_CAPTURE_POST_ROLL_MS";
export const VOICE_ACTIVITY_MIN_CONFIGURED_POST_ROLL_MS = 200;
export const VOICE_ACTIVITY_MAX_CONFIGURED_POST_ROLL_MS = 2_000;

export interface VoiceActiveChunkerOptions {
  emit: (pcm: Float32Array) => void;
  /**
   * 连续讲话时单个上传片段的上限（20 ms 帧）。默认 750 帧，即 15 秒。
   * 仅接受足以保留 pre-roll 与最少两帧有效语音的整数，非法值回退默认值。
   */
  maxChunkFrames?: number;
  /** 自然停顿切段所需的连续静音帧数；默认 60 帧，即 1.2 秒。 */
  postRollFrames?: number;
}

const MINIMUM_MAX_CHUNK_FRAMES =
  VOICE_ACTIVITY_PRE_ROLL_FRAMES + VOICE_ACTIVITY_MIN_FRAMES;

function resolveMaxChunkFrames(value: number | undefined): number {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= MINIMUM_MAX_CHUNK_FRAMES
    ? value
    : VOICE_ACTIVITY_MAX_CHUNK_FRAMES;
}

/** 将构建时 chunk 时长覆盖转换为 20ms 帧数；非法或越界值回退默认值。 */
export function resolveVoiceActivityMaxChunkFrames(
  rawValue: string | undefined,
): number {
  if (rawValue === undefined || rawValue.trim() === "") {
    return VOICE_ACTIVITY_MAX_CHUNK_FRAMES;
  }
  const milliseconds = Number(rawValue);
  if (!Number.isFinite(milliseconds)) return VOICE_ACTIVITY_MAX_CHUNK_FRAMES;
  if (
    milliseconds < VOICE_ACTIVITY_MIN_CONFIGURED_CHUNK_MS ||
    milliseconds > VOICE_ACTIVITY_MAX_CONFIGURED_CHUNK_MS
  ) {
    return VOICE_ACTIVITY_MAX_CHUNK_FRAMES;
  }
  const frames = Math.round(milliseconds / 20);
  return Number.isSafeInteger(frames) && frames >= MINIMUM_MAX_CHUNK_FRAMES
    ? frames
    : VOICE_ACTIVITY_MAX_CHUNK_FRAMES;
}

/** 将构建时 post-roll 覆盖转换为 20ms 帧数；非法或越界值回退默认值。 */
export function resolveVoiceActivityPostRollFrames(
  rawValue: string | undefined,
): number {
  if (rawValue === undefined || rawValue.trim() === "") {
    return VOICE_ACTIVITY_POST_ROLL_FRAMES;
  }
  const milliseconds = Number(rawValue);
  if (
    !Number.isFinite(milliseconds) ||
    milliseconds < VOICE_ACTIVITY_MIN_CONFIGURED_POST_ROLL_MS ||
    milliseconds > VOICE_ACTIVITY_MAX_CONFIGURED_POST_ROLL_MS
  ) {
    return VOICE_ACTIVITY_POST_ROLL_FRAMES;
  }
  const frames = Math.round(milliseconds / 20);
  return Number.isSafeInteger(frames) && frames >= VOICE_ACTIVITY_MIN_FRAMES
    ? frames
    : VOICE_ACTIVITY_POST_ROLL_FRAMES;
}

function frameRms(frame: Float32Array): number {
  let squareSum = 0;
  for (let index = 0; index < frame.length; index += 1) {
    squareSum += frame[index] * frame[index];
  }
  return Math.sqrt(squareSum / frame.length);
}

function concatFrames(frames: readonly Float32Array[]): Float32Array {
  const length = frames.reduce((sum, frame) => sum + frame.length, 0);
  const merged = new Float32Array(length);
  let offset = 0;
  for (const frame of frames) {
    merged.set(frame, offset);
    offset += frame.length;
  }
  return merged;
}

/**
 * 20 ms 帧级 VAD：
 * - 空闲时只保留短 pre-roll；全静音永不 emit。
 * - 连续讲话默认最多 15 秒一段，减少切片边界并避免远端 STT 侧稳定积压。
 * - 自然停顿必须达到默认 1.2 s post-roll 后才 emit；相邻 emitted chunks
 *   不复用样本。
 */
export class VoiceActiveChunker {
  private readonly options: VoiceActiveChunkerOptions;
  private readonly maxChunkFrames: number;
  private readonly postRollFrames: number;
  private pending = new Float32Array(0);
  private preRoll: Float32Array[] = [];
  private active: Float32Array[] = [];
  private activeSamples = 0;
  private activeVoiceFrames = 0;
  private trailingSilentFrames = 0;

  constructor(options: VoiceActiveChunkerOptions) {
    this.options = options;
    this.maxChunkFrames = resolveMaxChunkFrames(options.maxChunkFrames);
    this.postRollFrames =
      typeof options.postRollFrames === "number" &&
      Number.isSafeInteger(options.postRollFrames) &&
      options.postRollFrames >= VOICE_ACTIVITY_MIN_FRAMES
        ? options.postRollFrames
        : VOICE_ACTIVITY_POST_ROLL_FRAMES;
  }

  push(samples: Float32Array): void {
    if (samples.length === 0) return;
    const source = this.pending.length === 0
      ? samples
      : concatFrames([this.pending, samples]);
    let offset = 0;
    while (offset + VOICE_ACTIVITY_FRAME_SAMPLES <= source.length) {
      this.observeFrame(source.slice(offset, offset + VOICE_ACTIVITY_FRAME_SAMPLES));
      offset += VOICE_ACTIVITY_FRAME_SAMPLES;
    }
    this.pending = source.slice(offset);
  }

  /** 在采集自然结束时提交已验证的尾段；不足有效语音仍不上传。 */
  finish(): void {
    if (this.pending.length > 0) {
      const padded = new Float32Array(VOICE_ACTIVITY_FRAME_SAMPLES);
      padded.set(this.pending);
      this.observeFrame(padded);
      this.pending = new Float32Array(0);
    }
    if (this.activeVoiceFrames >= VOICE_ACTIVITY_MIN_FRAMES) {
      this.emitActive();
    } else {
      this.resetActiveToIdle();
    }
  }

  reset(): void {
    this.pending = new Float32Array(0);
    this.preRoll = [];
    this.active = [];
    this.activeSamples = 0;
    this.activeVoiceFrames = 0;
    this.trailingSilentFrames = 0;
  }

  private observeFrame(frame: Float32Array): void {
    const voiced = frameRms(frame) >= VOICE_ACTIVITY_FRAME_RMS;
    if (this.active.length === 0) {
      if (!voiced) {
        this.rememberPreRoll(frame);
        return;
      }
      this.active = [...this.preRoll];
      this.activeSamples = this.active.length * VOICE_ACTIVITY_FRAME_SAMPLES;
      this.preRoll = [];
    }

    this.active.push(frame);
    this.activeSamples += frame.length;
    if (voiced) {
      this.activeVoiceFrames += 1;
      this.trailingSilentFrames = 0;
    } else {
      this.trailingSilentFrames += 1;
    }

    if (
      this.activeVoiceFrames >= VOICE_ACTIVITY_MIN_FRAMES &&
      this.activeSamples >=
        this.maxChunkFrames * VOICE_ACTIVITY_FRAME_SAMPLES
    ) {
      this.emitActive();
      return;
    }

    if (
      this.activeVoiceFrames >= VOICE_ACTIVITY_MIN_FRAMES &&
      this.trailingSilentFrames >= this.postRollFrames
    ) {
      this.emitActive();
      return;
    }

    if (
      this.activeVoiceFrames < VOICE_ACTIVITY_MIN_FRAMES &&
      this.trailingSilentFrames >= this.postRollFrames
    ) {
      this.resetActiveToIdle();
    }
  }

  private rememberPreRoll(frame: Float32Array): void {
    this.preRoll.push(frame);
    if (this.preRoll.length > VOICE_ACTIVITY_PRE_ROLL_FRAMES) {
      this.preRoll.shift();
    }
  }

  private emitActive(): void {
    this.options.emit(concatFrames(this.active));
    this.active = [];
    this.activeSamples = 0;
    this.activeVoiceFrames = 0;
    this.trailingSilentFrames = 0;
  }

  private resetActiveToIdle(): void {
    const tail = this.active.slice(-VOICE_ACTIVITY_PRE_ROLL_FRAMES);
    this.active = [];
    this.activeSamples = 0;
    this.activeVoiceFrames = 0;
    this.trailingSilentFrames = 0;
    this.preRoll = tail;
  }
}
