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
  CAPTURE_CHUNK_SECONDS,
  CAPTURE_SAMPLE_RATE,
  downsample,
  floatTo16BitPCM,
  pcm16ToWav,
} from "@/capture/pcm";
import type { CaptureState } from "@/domain/session";
import { isNativeMobile } from "@/runtime";
import { registerPlugin, type PluginListenerHandle } from "@capacitor/core";
import { backendBase } from "@/runtime";
import { ensureServerSession } from "@/session";
import { captureDeviceId } from "@/capture/captureDeviceIdentity";
import {
  captureCorrelationSessionSalt,
} from "@/capture/captureCorrelation";
import {
  normalizeNativeCaptureUpload,
  type NativeCaptureUploadResult,
} from "@/capture/captureNativeBridge";

export type CaptureChunkHandler = (wav: Blob) => void | Promise<void>;
export type CaptureStatusHandler = (state: CaptureState, errorMessage?: string) => void;

const RETRY_MS = 5_000;
const NATIVE_RUNTIME_RETRY_LIMIT = 3;
const TV_SILENT_INPUT_GRACE_MS = 30_000;
const TV_SILENT_PEAK_THRESHOLD = 0.000002;

interface EchoAudioChunkEvent {
  base64?: string;
  sampleRate: number;
  source?: string;
  rms?: number;
  peak?: number;
  nativeOwned?: boolean;
}

interface EchoAudioErrorEvent {
  message?: string;
  source?: string;
}

interface EchoAudioUploadSessionRequiredEvent {
  status: number;
}

interface EchoAudioUploadSucceededEvent {
  segmentCorrelation?: unknown;
  ambientStored?: unknown;
  ambientText?: unknown;
}

interface EchoAudioPlugin {
  configureSession(options: {
    baseUrl: string;
    sessionToken: string;
    deviceId: string;
    correlationSalt: string;
  }): Promise<unknown>;
  setCaptureMode(options: {
    formal: boolean;
    meetingId: string;
  }): Promise<unknown>;
  start(options: { sampleRate: number; chunkMs: number }): Promise<{
    sampleRate: number;
    source?: string;
  }>;
  stop(): Promise<void>;
  status(): Promise<{
    active?: boolean;
    foregroundService?: boolean;
    nativeUpload?: boolean;
    authBlocked?: boolean;
    queuedChunks?: number;
  }>;
  addListener(
    eventName: "chunk",
    listenerFunc: (event: EchoAudioChunkEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "error",
    listenerFunc: (event: EchoAudioErrorEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "uploadSessionRequired",
    listenerFunc: (event: EchoAudioUploadSessionRequiredEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "captureUploadSucceeded",
    listenerFunc: (event: EchoAudioUploadSucceededEvent) => void,
  ): Promise<PluginListenerHandle>;
}

const EchoAudio = registerPlugin<EchoAudioPlugin>("EchoAudio");
const NATIVE_DEAD_INPUT_RMS_THRESHOLD = 1;
const NATIVE_DEAD_INPUT_PEAK_THRESHOLD = 4;

function isAndroidTvRuntime(): boolean {
  if (typeof window === "undefined" || typeof document === "undefined") return false;
  return (
    /Android/i.test(window.navigator.userAgent) &&
    document.documentElement.classList.contains("echodesk-tv")
  );
}

function isNativeAndroidRuntime(): boolean {
  if (typeof window === "undefined") return false;
  return isNativeMobile() && /Android/i.test(window.navigator.userAgent);
}

function shouldUseNativeAudioRecord(): boolean {
  return isNativeAndroidRuntime();
}

function nativeSilentProbeSummary(message: string): string | null {
  const summary = message.match(/Probe summary:\s*([^.]*)/i)?.[1]?.trim();
  if (!summary) return null;
  return summary;
}

function blobFromBase64Wav(base64: string): Blob {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) {
    bytes[i] = bin.charCodeAt(i);
  }
  return new Blob([bytes], { type: "audio/wav" });
}

function buildAudioConstraints(): MediaStreamConstraints["audio"] {
  if (isAndroidTvRuntime()) {
    // Android TV WebView/Audio HAL 的兼容性弱于桌面浏览器：部分机型对
    // sampleRate/AGC/NS 约束会返回静音或直接失败。TV 端只要求单声道，
    // 让系统选择可用输入参数，后续仍统一下采样到 16k WAV。
    return {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    };
  }
  return {
    channelCount: 1,
    sampleRate: CAPTURE_SAMPLE_RATE,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  };
}

function getUserMediaWithTimeout(
  constraints: MediaStreamConstraints,
  timeoutMs: number,
  label: string,
): Promise<MediaStream> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`${label}超时（${Math.round(timeoutMs / 1000)} 秒）`));
    }, timeoutMs);

    navigator.mediaDevices
      .getUserMedia(constraints)
      .then((stream) => {
        if (settled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        settled = true;
        window.clearTimeout(timer);
        resolve(stream);
      })
      .catch((err: unknown) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        reject(err);
      });
  });
}

async function requestElectronMicAccess(): Promise<void> {
  try {
    await window.echo?.requestMic?.();
  } catch {
    /* Electron IPC 不可用时继续走浏览器 getUserMedia。 */
  }
}

async function listAudioInputDevices(): Promise<MediaDeviceInfo[]> {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((device) => device.kind === "audioinput");
  } catch {
    return [];
  }
}

function normalizeDesktopMicError(
  error: unknown,
  audioInputs: MediaDeviceInfo[],
): string {
  const raw =
    error instanceof Error
      ? `${error.name}: ${error.message}`
      : String(error);
  if (/notfounderror|requested device not found|device not found/i.test(raw)) {
    if (audioInputs.length === 0) {
      return "系统已授权，但 EchoDesk 没有枚举到任何麦克风输入。请到 系统设置 → 隐私与安全 → 麦克风 关闭后重新勾选 EchoDesk，或完全退出后重开 EchoDesk。";
    }
    return `找不到可用麦克风输入。当前可见输入：${audioInputs
      .map((device) => device.label || "未命名麦克风")
      .join("、")}`;
  }
  if (/notallowederror|permission denied|denied/i.test(raw)) {
    return "麦克风权限被拒绝。请到 系统设置 → 隐私与安全 → 麦克风 勾选 EchoDesk。";
  }
  return raw;
}

class AudioCapture {
  private state: CaptureState = "initializing";
  private errorMessage: string | null = null;
  private chunkHandlers = new Set<CaptureChunkHandler>();
  private statusHandlers = new Set<CaptureStatusHandler>();
  private audioCtx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private proc: ScriptProcessorNode | null = null;
  private nativePlugin: EchoAudioPlugin = EchoAudio;
  private nativeHandles: PluginListenerHandle[] = [];
  private nativeActive = false;
  private nativeAttemptGeneration = 0;
  private nativeRuntimeRetryAttempts = 0;
  private nativeCleanup: Promise<void> = Promise.resolve();
  private nativeSilentChunks = 0;
  private nativeSessionHandle: PluginListenerHandle | null = null;
  private nativeUploadHandle: PluginListenerHandle | null = null;
  private nativeSessionRecovery: Promise<void> | null = null;
  private nativeUploadHandlers = new Set<(result: NativeCaptureUploadResult) => void>();
  private buf: Float32Array[] = [];
  private accSamples = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private running = false;
  private generation = 0;
  private firstFrameGeneration = -1;
  private silentInputSinceMs: number | null = null;

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

  onNativeUpload(handler: (result: NativeCaptureUploadResult) => void): () => void {
    this.nativeUploadHandlers.add(handler);
    return () => this.nativeUploadHandlers.delete(handler);
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.generation += 1;
    this.firstFrameGeneration = -1;
    this.nativeRuntimeRetryAttempts = 0;
    void this.boot(this.generation);
  }

  stop(): void {
    if (shouldUseNativeAudioRecord()) {
      // A fresh renderer cannot trust its in-memory nativeActive flag.
      void this.nativePlugin.stop().catch(() => undefined);
    }
    if (
      !this.running &&
      this.stream === null &&
      !this.nativeActive &&
      this.nativeHandles.length === 0
    ) {
      return;
    }
    this.running = false;
    this.generation += 1;
    this.nativeRuntimeRetryAttempts = 0;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.teardown();
    this.setState("initializing");
  }

  async attachNativeRuntime(): Promise<void> {
    if (!shouldUseNativeAudioRecord()) return;
    const plugin = this.nativePlugin;
    if (!this.nativeSessionHandle) {
      this.nativeSessionHandle = await plugin.addListener(
        "uploadSessionRequired",
        (event) => {
          if (event.status === 409) {
            window.dispatchEvent(new Event("echodesk:capture-control-refresh"));
            return;
          }
          if (this.nativeSessionRecovery) return;
          this.nativeSessionRecovery = this.configureNativeUploadSession(
            event.status === 401 || event.status === 403,
          )
            .catch((error: unknown) => {
              const detail = error instanceof Error ? error.message : String(error);
              this.setState("error", `收音身份恢复失败：${detail}`);
            })
            .finally(() => {
              this.nativeSessionRecovery = null;
            });
        },
      );
    }
    if (!this.nativeUploadHandle) {
      this.nativeUploadHandle = await plugin.addListener(
        "captureUploadSucceeded",
        (event) => {
          const result = normalizeNativeCaptureUpload(event);
          if (!result) return;
          for (const handler of this.nativeUploadHandlers) handler(result);
        },
      );
    }
    await this.configureNativeUploadSession(false);
  }

  private async configureNativeUploadSession(forceRenew: boolean): Promise<void> {
    const [baseUrl, sessionToken] = await Promise.all([
      backendBase(),
      ensureServerSession(forceRenew),
    ]);
    if (!sessionToken) throw new Error("无法建立收音上传会话");
    await this.nativePlugin.configureSession({
      baseUrl,
      sessionToken,
      deviceId: captureDeviceId(),
      correlationSalt: captureCorrelationSessionSalt(),
    });
    window.dispatchEvent(new Event("echodesk:capture-control-refresh"));
  }

  private setState(next: CaptureState, errorMessage?: string): void {
    this.state = next;
    this.errorMessage = errorMessage ?? null;
    for (const h of this.statusHandlers) h(next, errorMessage);
  }

  private emitChunk(wav: Blob): void {
    this.firstFrameGeneration = this.generation;
    for (const h of this.chunkHandlers) h(wav);
  }

  hasFirstFrame(): boolean {
    return this.running && this.firstFrameGeneration === this.generation;
  }

  waitForFirstFrame(timeoutMs = 18_000): Promise<void> {
    if (this.hasFirstFrame()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const off = this.onChunk(() => {
        window.clearTimeout(timer);
        off();
        resolve();
      });
      const timer = window.setTimeout(() => {
        off();
        reject(new Error("麦克风首帧超时"));
      }, timeoutMs);
    });
  }

  async setFormalMode(meetingId: string | null): Promise<void> {
    if (!shouldUseNativeAudioRecord()) return;
    await this.nativePlugin.setCaptureMode({
      formal: meetingId !== null,
      meetingId: meetingId ?? "",
    });
  }

  private teardown(): void {
    this.teardownNative();
    this.proc?.disconnect();
    this.proc = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.audioCtx?.close().catch(() => undefined);
    this.audioCtx = null;
    this.buf = [];
    this.accSamples = 0;
    this.silentInputSinceMs = null;
  }

  private teardownNative(): void {
    this.nativeAttemptGeneration += 1;
    const handles = this.nativeHandles;
    const shouldStop = this.nativeActive || handles.length > 0;
    const plugin = this.nativePlugin;
    this.nativeActive = false;
    this.nativeSilentChunks = 0;
    this.nativeHandles = [];
    if (!shouldStop) return;

    const previousCleanup = this.nativeCleanup;
    this.nativeCleanup = (async () => {
      await previousCleanup.catch(() => undefined);
      await Promise.allSettled(handles.map((handle) => handle.remove()));
      await plugin.stop().catch(() => undefined);
    })();
  }

  private scheduleRetry(): void {
    if (!this.running) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    const generation = this.generation;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.boot(generation);
    }, RETRY_MS);
  }

  private isCurrent(generation: number): boolean {
    return this.running && this.generation === generation;
  }

  private observeInputHealth(input: Float32Array): boolean {
    if (!isAndroidTvRuntime()) return true;

    let peak = 0;
    for (let i = 0; i < input.length; i += 1) {
      const v = Math.abs(input[i]);
      if (v > peak) peak = v;
    }

    if (peak > TV_SILENT_PEAK_THRESHOLD) {
      this.silentInputSinceMs = null;
      return true;
    }

    const now = Date.now();
    this.silentInputSinceMs ??= now;
    if (now - this.silentInputSinceMs < TV_SILENT_INPUT_GRACE_MS) {
      return true;
    }

    this.setState(
      "error",
      "电视麦克风没有有效输入；请确认电视/遥控器麦克风或外接会议麦克风已被系统识别",
    );
    this.teardown();
    this.scheduleRetry();
    return false;
  }

  private handleNativeFailure(
    message: string,
    generation: number,
    attemptGeneration: number,
  ): void {
    if (
      !this.isCurrent(generation) ||
      this.nativeAttemptGeneration !== attemptGeneration
    ) {
      return;
    }
    const shouldRetry =
      this.nativeRuntimeRetryAttempts < NATIVE_RUNTIME_RETRY_LIMIT;
    if (shouldRetry) this.nativeRuntimeRetryAttempts += 1;
    this.setState(
      "error",
      shouldRetry
        ? message
        : `${message}；自动恢复已达 ${NATIVE_RUNTIME_RETRY_LIMIT} 次上限，请检查麦克风后手动重试`,
    );
    this.teardownNative();
    if (shouldRetry) this.scheduleRetry();
  }

  private observeNativeInputHealth(
    event: EchoAudioChunkEvent,
    generation: number,
    attemptGeneration: number,
  ): boolean {
    const rms = event.rms ?? 0;
    const peak = event.peak ?? 0;
    if (rms > NATIVE_DEAD_INPUT_RMS_THRESHOLD || peak > NATIVE_DEAD_INPUT_PEAK_THRESHOLD) {
      this.silentInputSinceMs = null;
      this.nativeSilentChunks = 0;
      this.nativeRuntimeRetryAttempts = 0;
      if (this.state !== "capturing") {
        this.setState("capturing");
      }
      return true;
    }

    const now = Date.now();
    this.silentInputSinceMs ??= now;
    this.nativeSilentChunks += 1;
    if (this.state !== "capturing") {
      this.setState("capturing");
    }
    if (now - this.silentInputSinceMs < TV_SILENT_INPUT_GRACE_MS) {
      return true;
    }

    this.handleNativeFailure(
      "Android/TV 麦克风持续返回全静音；请确认电视麦克风已开启，或接入 USB/蓝牙会议麦克风",
      generation,
      attemptGeneration,
    );
    return false;
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

  __setNativePluginForTest(plugin: EchoAudioPlugin): void {
    if (!import.meta.env.DEV) {
      throw new Error("Native audio plugin injection is available in dev/test only");
    }
    this.stop();
    this.nativePlugin = plugin;
    this.nativeRuntimeRetryAttempts = 0;
  }

  private async boot(generation: number): Promise<void> {
    if (!this.isCurrent(generation)) return;
    this.setState("initializing");
    this.teardown();
    if (shouldUseNativeAudioRecord()) {
      await this.bootNative(generation);
      return;
    }
    try {
      await requestElectronMicAccess();
      if (!this.isCurrent(generation)) return;
      let audioInputs = await listAudioInputDevices();
      if (!this.isCurrent(generation)) return;
      let stream: MediaStream;
      try {
        stream = await getUserMediaWithTimeout(
          {
            audio: buildAudioConstraints(),
            video: false,
          },
          12_000,
          "麦克风初始化",
        );
      } catch (firstError) {
        console.warn("[audio-capture] constrained getUserMedia failed:", firstError);
        audioInputs = await listAudioInputDevices();
        try {
          stream = await getUserMediaWithTimeout(
            { audio: true, video: false },
            12_000,
            "默认麦克风初始化",
          );
        } catch (fallbackError) {
          throw new Error(normalizeDesktopMicError(fallbackError, audioInputs));
        }
      }
      if (!this.isCurrent(generation)) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.stream = stream;
      const selectedTrack = stream.getAudioTracks()[0];
      if (selectedTrack) {
        const selectedSettings = selectedTrack.getSettings();
        // 不记录 deviceId/groupId（稳定标识不应进入日志）；label 足以判断是否
        // 意外选中了 BlackHole/显示器/虚拟声卡等非预期输入。
        console.info("[audio-capture] selected input", {
          label: selectedTrack.label || "unnamed audio input",
          sampleRate: selectedSettings.sampleRate,
          channelCount: selectedSettings.channelCount,
          echoCancellation: selectedSettings.echoCancellation,
          noiseSuppression: selectedSettings.noiseSuppression,
          autoGainControl: selectedSettings.autoGainControl,
        });
      }

      const ctx = isAndroidTvRuntime()
        ? new AudioContext()
        : new AudioContext({ sampleRate: CAPTURE_SAMPLE_RATE });
      this.audioCtx = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      this.proc = proc;

      proc.onaudioprocess = (ev) => {
        const ch = ev.inputBuffer.getChannelData(0);
        if (!this.observeInputHealth(ch)) return;
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
      if (!this.isCurrent(generation)) return;
      const msg = e instanceof Error ? e.message : String(e);
      this.setState("error", msg);
      this.scheduleRetry();
    }
  }

  private async bootNative(generation: number): Promise<void> {
    await this.nativeCleanup.catch(() => undefined);
    if (!this.isCurrent(generation)) return;
    const plugin = this.nativePlugin;
    const attemptGeneration = ++this.nativeAttemptGeneration;
    const isActiveAttempt = (): boolean =>
      this.isCurrent(generation) &&
      this.nativeAttemptGeneration === attemptGeneration;

    try {
      await this.attachNativeRuntime();
      const chunkHandle = await plugin.addListener("chunk", (event) => {
        if (!this.nativeActive || !isActiveAttempt()) return;
        if (!this.observeNativeInputHealth(event, generation, attemptGeneration)) return;
        if (event.nativeOwned) {
          this.firstFrameGeneration = this.generation;
          return;
        }
        if (!event.base64) return;
        this.emitChunk(blobFromBase64Wav(event.base64));
      });
      if (!isActiveAttempt()) {
        await chunkHandle.remove();
        return;
      }
      this.nativeHandles.push(chunkHandle);

      const errorHandle = await plugin.addListener("error", (event) => {
        if (!this.nativeActive || !isActiveAttempt()) return;
        const msg =
          event.message ||
          "Android 原生录音失败，请接入 USB/蓝牙会议麦克风";
        this.handleNativeFailure(msg, generation, attemptGeneration);
      });
      if (!isActiveAttempt()) {
        await errorHandle.remove();
        return;
      }
      this.nativeHandles.push(errorHandle);
      this.nativeActive = true;
      await plugin.start({
        sampleRate: CAPTURE_SAMPLE_RATE,
        chunkMs: CAPTURE_CHUNK_SECONDS * 1000,
      });
      if (!isActiveAttempt()) {
        this.teardownNative();
        await plugin.stop().catch(() => undefined);
        return;
      }
      this.setState("capturing");
    } catch (e) {
      if (!this.isCurrent(generation)) return;
      if (this.nativeAttemptGeneration !== attemptGeneration) return;
      const msg = e instanceof Error ? e.message : String(e);
      const noUsableInput =
        /silent PCM|every source returned silent|microphone sources/i.test(msg);
      const probeSummary = nativeSilentProbeSummary(msg);
      const errorMessage = noUsableInput
        ? probeSummary
          ? `电视没有提供有效麦克风输入（${probeSummary}）；请接入 USB/蓝牙会议麦克风后重新打开 EchoDesk`
          : "电视没有提供有效麦克风输入；请接入 USB/蓝牙会议麦克风后重新打开 EchoDesk"
        : `Android 原生录音不可用：${msg}。请接入 USB/蓝牙会议麦克风`;
      if (noUsableInput) {
        this.setState("error", errorMessage);
        this.teardownNative();
      } else {
        this.handleNativeFailure(errorMessage, generation, attemptGeneration);
      }
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
