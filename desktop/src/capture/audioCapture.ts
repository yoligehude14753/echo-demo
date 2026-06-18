/**
 * AudioCapture — CaptureSession 实现（24/7 持续采集）
 *
 * 职责边界：
 * - 只管 getUserMedia + PCM 切片 + 产出 wav Blob
 * - 不知道 meeting_id、不上传、不提供 UI
 * - App 启动时 start()，退出时 stop()
 */
import {
  CAPTURE_CHUNK_SAMPLES,
  CAPTURE_SAMPLE_RATE,
  downsample,
  floatTo16BitPCM,
  pcm16ToWav,
} from "@/capture/pcm";
import type { CaptureState } from "@/domain/session";

export type CaptureChunkHandler = (wav: Blob) => void;
export type CaptureStatusHandler = (state: CaptureState, errorMessage?: string) => void;

const RETRY_MS = 5_000;

class AudioCapture {
  private state: CaptureState = "initializing";
  private errorMessage: string | null = null;
  private chunkHandlers = new Set<CaptureChunkHandler>();
  private statusHandlers = new Set<CaptureStatusHandler>();
  private audioCtx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private proc: ScriptProcessorNode | null = null;
  private buf: Float32Array[] = [];
  private accSamples = 0;
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

  private teardown(): void {
    this.proc?.disconnect();
    this.proc = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.audioCtx?.close().catch(() => undefined);
    this.audioCtx = null;
    this.buf = [];
    this.accSamples = 0;
  }

  private scheduleRetry(): void {
    if (!this.running) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => void this.boot(), RETRY_MS);
  }

  private flush(force = false): void {
    if (!force && this.accSamples < CAPTURE_CHUNK_SAMPLES) return;
    if (this.buf.length === 0) return;

    const total = this.buf.reduce((s, b) => s + b.length, 0);
    const merged = new Float32Array(total);
    let off = 0;
    for (const b of this.buf) {
      merged.set(b, off);
      off += b.length;
    }
    this.buf = [];
    this.accSamples = 0;

    const ctx = this.audioCtx;
    if (!ctx) return;
    const down = downsample(merged, ctx.sampleRate, CAPTURE_SAMPLE_RATE);
    const pcm = floatTo16BitPCM(down);
    this.emitChunk(pcm16ToWav(pcm, CAPTURE_SAMPLE_RATE));
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
          autoGainControl: true,
        },
        video: false,
      });
      this.stream = stream;

      const ctx = new AudioContext();
      this.audioCtx = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      this.proc = proc;

      proc.onaudioprocess = (ev) => {
        const ch = ev.inputBuffer.getChannelData(0);
        this.buf.push(new Float32Array(ch));
        this.accSamples += Math.round((ch.length * CAPTURE_SAMPLE_RATE) / ctx.sampleRate);
        if (this.accSamples >= CAPTURE_CHUNK_SAMPLES) {
          this.flush();
        }
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
