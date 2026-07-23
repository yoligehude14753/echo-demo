/**
 * 自由收音的本地语音片段器。
 *
 * 仅在含有足够有效语音的片段完成时回调；静音不会产生上传载荷。这里输出的
 * 仍是原始 16 kHz PCM，WAV 封装和 HTTP 合约继续由既有 Capture 层处理。
 */

export const VOICE_ACTIVITY_SAMPLE_RATE = 16_000;
export const VOICE_ACTIVITY_FRAME_SAMPLES = 320; // 20 ms
export const VOICE_ACTIVITY_FRAME_RMS = 800 / 32_767;
export const VOICE_ACTIVITY_MIN_FRAMES = 2;
export const VOICE_ACTIVITY_PRE_ROLL_FRAMES = 6; // 120 ms
export const VOICE_ACTIVITY_POST_ROLL_FRAMES = 8; // 160 ms
export const VOICE_ACTIVITY_MAX_CHUNK_FRAMES = 32; // 640 ms

export const VOICE_ACTIVITY_MAX_CHUNK_MS =
  (VOICE_ACTIVITY_MAX_CHUNK_FRAMES * VOICE_ACTIVITY_FRAME_SAMPLES * 1_000) /
  VOICE_ACTIVITY_SAMPLE_RATE;
// ScriptProcessor 的 4096-sample callback 在 16 kHz 下最多约 256 ms；加上
// 640 ms 成段上限，首次有效声音到本地 emit 的上界为约 900 ms。
export const VOICE_ACTIVITY_MAX_CLIENT_EMISSION_LATENCY_MS = 900;

export interface VoiceActiveChunkerOptions {
  emit: (pcm: Float32Array) => void;
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
 * - 连续讲话最多 640 ms 一段，避免再等待多秒整窗。
 * - 短句在 160 ms post-roll 后 emit；相邻 emitted chunks 不复用样本。
 */
export class VoiceActiveChunker {
  private readonly options: VoiceActiveChunkerOptions;
  private pending = new Float32Array(0);
  private preRoll: Float32Array[] = [];
  private active: Float32Array[] = [];
  private activeSamples = 0;
  private activeVoiceFrames = 0;
  private trailingSilentFrames = 0;

  constructor(options: VoiceActiveChunkerOptions) {
    this.options = options;
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
        VOICE_ACTIVITY_MAX_CHUNK_FRAMES * VOICE_ACTIVITY_FRAME_SAMPLES
    ) {
      this.emitActive();
      return;
    }

    if (
      this.activeVoiceFrames >= VOICE_ACTIVITY_MIN_FRAMES &&
      this.trailingSilentFrames >= VOICE_ACTIVITY_POST_ROLL_FRAMES
    ) {
      this.emitActive();
      return;
    }

    if (
      this.activeVoiceFrames < VOICE_ACTIVITY_MIN_FRAMES &&
      this.trailingSilentFrames >= VOICE_ACTIVITY_POST_ROLL_FRAMES
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
