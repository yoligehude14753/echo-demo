/**
 * AudioCapture — CaptureSession 实现（24/7 持续采集）
 *
 * 职责边界：
 * - 只管 getUserMedia + PCM 切片 + 产出 wav Blob
 * - 不知道 meeting_id、不上传、不提供 UI
 * - App 启动时 start()，退出时 stop()
 */
import {
  CAPTURE_SAMPLE_RATE,
  downsample,
  floatTo16BitPCM,
  pcm16ToWav,
} from "@/capture/pcm";
import type { CaptureState } from "@/domain/session";

export type CaptureChunkHandler = (wav: Blob) => void;
export type CaptureStatusHandler = (state: CaptureState, errorMessage?: string) => void;
/** turn 结束信号：说话后静音达到阈值 → 说话人这一轮"说完了"。 */
export type CaptureEndpointHandler = () => void;

const RETRY_MS = 5_000;

// ── VAD endpointing 参数（替代固定 4s 硬切，避免切断词/丢开头）──────────
// 帧级 RMS 高于此值算"有语音"（Float32 [-1,1]；安静底噪 ~0.002，说话 ~0.02+）。
const VAD_SPEECH_RMS = 0.012;
// 说完后的静音多久就判定一句结束并切段（断句停顿）。
const VAD_SILENCE_FLUSH_MS = 650;
// 一段语音的最短时长（短于此的当作噪点丢弃，不触发 STT）。
const VAD_MIN_UTTER_MS = 300;
// 一段语音的最长时长（连续说话不停时强制切段，给 STT 一个上界）。
const VAD_MAX_UTTER_MS = 12_000;
// onset 前预留的音频（保证开头第一个字/"echo"不被削掉）。
const VAD_PREROLL_MS = 400;
// 说话后连续静音达到此值 → 判定"这一轮说完了"，发 endpoint 信号（自适应断点）。
// 1.6s：短于此的停顿视为句间换气/思考（继续累积），长于此视为说完（尽快执行）。
const VAD_ENDPOINT_SILENCE_MS = 1_600;

class AudioCapture {
  private state: CaptureState = "initializing";
  private errorMessage: string | null = null;
  private chunkHandlers = new Set<CaptureChunkHandler>();
  private statusHandlers = new Set<CaptureStatusHandler>();
  private endpointHandlers = new Set<CaptureEndpointHandler>();
  private audioCtx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private proc: ScriptProcessorNode | null = null;
  // VAD endpointing 状态
  private preRoll: Float32Array[] = []; // onset 前的滚动预留帧
  private preRollMs = 0;
  private speechBuf: Float32Array[] = []; // 当前一句累积的帧
  private inSpeech = false;
  private speechMs = 0;
  private silenceMs = 0;
  // turn-endpoint：自上次出现语音帧以来的累计静音；spokeThisTurn 标记本轮说过话。
  private postSpeechSilenceMs = 0;
  private spokeThisTurn = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private running = false;

  getState(): CaptureState {
    return this.state;
  }

  getErrorMessage(): string | null {
    return this.errorMessage;
  }

  onChunk(handler: CaptureChunkHandler): () => void {
    this.chunkHandlers.add(handler);
    return () => this.chunkHandlers.delete(handler);
  }

  onStatus(handler: CaptureStatusHandler): () => void {
    this.statusHandlers.add(handler);
    handler(this.state, this.errorMessage ?? undefined);
    return () => this.statusHandlers.delete(handler);
  }

  onEndpoint(handler: CaptureEndpointHandler): () => void {
    this.endpointHandlers.add(handler);
    return () => this.endpointHandlers.delete(handler);
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    void this.boot();
  }

  stop(): void {
    this.running = false;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.teardown();
    this.setState("initializing");
  }

  private setState(next: CaptureState, errorMessage?: string): void {
    this.state = next;
    this.errorMessage = errorMessage ?? null;
    for (const h of this.statusHandlers) h(next, errorMessage);
  }

  private emitChunk(wav: Blob): void {
    for (const h of this.chunkHandlers) h(wav);
  }

  private emitEndpoint(): void {
    for (const h of this.endpointHandlers) h();
  }

  private teardown(): void {
    this.proc?.disconnect();
    this.proc = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.audioCtx?.close().catch(() => undefined);
    this.audioCtx = null;
    this.preRoll = [];
    this.preRollMs = 0;
    this.speechBuf = [];
    this.inSpeech = false;
    this.speechMs = 0;
    this.silenceMs = 0;
    this.postSpeechSilenceMs = 0;
    this.spokeThisTurn = false;
  }

  private scheduleRetry(): void {
    if (!this.running) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => void this.boot(), RETRY_MS);
  }

  private static rms(frame: Float32Array): number {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    return frame.length > 0 ? Math.sqrt(sum / frame.length) : 0;
  }

  /** 把当前累积的一句语音切段送出（不足最短时长则丢弃）。 */
  private flush(): void {
    const ctx = this.audioCtx;
    const buf = this.speechBuf;
    const speechMs = this.speechMs;
    this.speechBuf = [];
    this.inSpeech = false;
    this.speechMs = 0;
    this.silenceMs = 0;
    if (!ctx || buf.length === 0 || speechMs < VAD_MIN_UTTER_MS) return;

    const total = buf.reduce((s, b) => s + b.length, 0);
    const merged = new Float32Array(total);
    let off = 0;
    for (const b of buf) {
      merged.set(b, off);
      off += b.length;
    }
    const down = downsample(merged, ctx.sampleRate, CAPTURE_SAMPLE_RATE);
    const pcm = floatTo16BitPCM(down);
    this.emitChunk(pcm16ToWav(pcm, CAPTURE_SAMPLE_RATE));
  }

  /** 处理一帧音频：VAD 断句（有语音累积、停顿切段），保证一句完整、不丢开头。 */
  private onFrame(frame: Float32Array, frameMs: number): void {
    const isSpeech = AudioCapture.rms(frame) >= VAD_SPEECH_RMS;

    // 维护 onset 前预留帧（滚动窗口）；所有帧等长，每帧计 frameMs。
    this.preRoll.push(frame);
    this.preRollMs += frameMs;
    while (this.preRollMs > VAD_PREROLL_MS && this.preRoll.length > 1) {
      this.preRoll.shift();
      this.preRollMs -= frameMs;
    }

    if (isSpeech) {
      this.postSpeechSilenceMs = 0;
      this.spokeThisTurn = true;
      if (!this.inSpeech) {
        // 语音起点：把 pre-roll 一起带上，保证开头第一个字不被削
        this.inSpeech = true;
        this.speechBuf = [...this.preRoll];
        this.speechMs = this.preRollMs;
      } else {
        this.speechBuf.push(frame);
        this.speechMs += frameMs;
      }
      this.silenceMs = 0;
      if (this.speechMs >= VAD_MAX_UTTER_MS) this.flush();
      return;
    }

    // 静音帧
    if (this.inSpeech) {
      this.speechBuf.push(frame); // 保留尾部静音让 STT 收尾更稳
      this.speechMs += frameMs;
      this.silenceMs += frameMs;
      if (this.silenceMs >= VAD_SILENCE_FLUSH_MS) this.flush(); // 一句说完
    }

    // turn-endpoint：累计本轮语音之后的静音；达阈值且本轮说过话 → 发一次结束信号
    if (this.spokeThisTurn) {
      this.postSpeechSilenceMs += frameMs;
      if (this.postSpeechSilenceMs >= VAD_ENDPOINT_SILENCE_MS) {
        this.spokeThisTurn = false;
        this.postSpeechSilenceMs = 0;
        this.emitEndpoint();
      }
    }
  }

  /**
   * Test seam：让 E2E 跳过 ~6s 真实音频积累，直接合成一次 chunk emit。
   * Headless Chromium 拿不到真实麦克风、AudioContext 也不跑，无法验证
   * Phase 4「采集 vs 入库」两个计数器；E2E 通过 `window.__echoAudioCapture`
   * 调用本方法触发 ChunkRouter。production 永不调用。
   */
  __emitChunkForTest(blob?: Blob): void {
    const payload = blob ?? new Blob([new Uint8Array(44)], { type: "audio/wav" });
    this.emitChunk(payload);
  }

  private async boot(): Promise<void> {
    if (!this.running) return;
    this.setState("initializing");
    this.teardown();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: CAPTURE_SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
        },
        video: false,
      });
      this.stream = stream;

      const ctx = new AudioContext();
      this.audioCtx = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      this.proc = proc;

      const frameMs = (4096 / ctx.sampleRate) * 1000;
      proc.onaudioprocess = (ev) => {
        const ch = ev.inputBuffer.getChannelData(0);
        this.onFrame(new Float32Array(ch), frameMs);
      };
      src.connect(proc);
      proc.connect(ctx.destination);

      this.setState("capturing");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      this.setState("error", msg);
      this.scheduleRetry();
    }
  }
}

/** 全局单例：CaptureSession 在 runtime 层唯一实例 */
export const audioCapture = new AudioCapture();

// 仅 dev/test 暴露给 window；production build (import.meta.env.DEV=false) 不挂。
// 见 src/vite-env.d.ts —— /// <reference types="vite/client" /> 让 import.meta.env 通过类型校验。
if (import.meta.env.DEV && typeof window !== "undefined") {
  (
    window as Window & { __echoAudioCapture?: AudioCapture }
  ).__echoAudioCapture = audioCapture;
}
