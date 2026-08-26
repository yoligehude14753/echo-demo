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
import {
  VOICE_ACTIVITY_FRAME_RMS,
  VOICE_ACTIVITY_CHUNK_MS_ENV,
  VOICE_ACTIVITY_POST_ROLL_MS_ENV,
  resolveVoiceActivityMaxChunkFrames,
  resolveVoiceActivityPostRollFrames,
  VoiceActiveChunker,
} from "@/capture/voiceActiveChunker";
import {
  AudioCaptureStateMachine,
  type AudioCaptureSnapshot,
  type AudioCaptureSnapshotHandler,
} from "@/capture/audioCaptureState";
import { isNativeMobile } from "@/runtime";
import { registerPlugin, type PluginListenerHandle } from "@capacitor/core";
import { backendBase } from "@/runtime";
import { ensureServerSession } from "@/session";
import { captureDeviceId } from "@/capture/captureDeviceIdentity";
import {
  createCaptureScope,
  type CaptureScope,
} from "@/capture/captureScope";
import {
  captureCorrelationSessionSalt,
} from "@/capture/captureCorrelation";
import { applyBoundedCaptureAgc } from "@/capture/captureInputAgc";
import {
  normalizeNativeCaptureUpload,
  type NativeCaptureUploadResult,
} from "@/capture/captureNativeBridge";

export type CaptureChunkHandler = (wav: Blob) => void | Promise<void>;
export type CaptureStatusHandler = AudioCaptureSnapshotHandler;

export interface AudioCaptureDiagnostics {
  audioContextState: AudioContextState | "none";
  appTrackLive: boolean;
  selectedDeviceExists: boolean;
  selectedDeviceIsDefault: boolean;
  selectionAuthorized: boolean;
  rawFrames: number;
  realFrames: number;
  voicedFrames: number;
  maxRms: number;
  maxPeak: number;
  chunkProduced: number;
  captureState: AudioCaptureSnapshot["state"];
  lastErrorCode: string | null;
  gainApplied: number;
  maxGain: number;
  clippedSamples: number;
  lastAudioCallbackAgeMs: number | null;
}

const RETRY_MS = 5_000;
const CAPTURE_INIT_WATCHDOG_MS = 18_000;
const WEB_AUDIO_LIVENESS_CHECK_MS = 2_000;
const WEB_AUDIO_CALLBACK_STALE_MS = 8_000;
const ELECTRON_MIC_PREFLIGHT_TIMEOUT_MS = 3_000;
const MIC_INIT_TIMEOUT_MESSAGE =
  "系统录音初始化超时；问答、知识库、联网搜索和文档生成仍可继续使用，请稍后重新打开 EchoDesk 或检查 macOS 麦克风权限。";
// Android NativeAudioGate 独立使用 20 ms VAD，并保持 640 ms 单片硬上限；
// 桌面 WebAudio cadence 调整不得扩大原生片段或其上传延迟。
const NATIVE_CAPTURE_CHUNK_MS = 640;
const WEB_AUDIO_PROCESSOR_BUFFER_SAMPLES = 1_024;
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
    captureSessionId: string;
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

async function settleElectronMicIpc<T>(
  operation: (() => Promise<T>) | undefined,
): Promise<T | undefined> {
  if (!operation) return undefined;
  return await new Promise<T | undefined>((resolve) => {
    let settled = false;
    const timer = window.setTimeout(() => {
      settled = true;
      resolve(undefined);
    }, ELECTRON_MIC_PREFLIGHT_TIMEOUT_MS);
    operation()
      .then((value) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(value);
      })
      .catch(() => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(undefined);
      });
  });
}

async function requestElectronMicAccess(): Promise<void> {
  const status = await settleElectronMicIpc(window.echo?.getMicStatus);
  if (status === "granted" || status === "denied" || status === "restricted") {
    return;
  }
  // askForMediaAccess can remain pending when macOS already has a stale TCC
  // row after an ad-hoc candidate replacement.  Bound this preflight so the
  // browser-level getUserMedia path still gets a chance to resolve or report
  // its own actionable error within the capture initialization watchdog.
  await settleElectronMicIpc(window.echo?.requestMic);
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
  private lifecycle = new AudioCaptureStateMachine();
  private chunkHandlers = new Set<CaptureChunkHandler>();
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
  private scope = createCaptureScope(captureDeviceId());
  private voiceChunker = new VoiceActiveChunker({
    maxChunkFrames: resolveVoiceActivityMaxChunkFrames(
      (import.meta as { env?: Record<string, string> }).env?.[
        VOICE_ACTIVITY_CHUNK_MS_ENV
      ],
    ),
    postRollFrames: resolveVoiceActivityPostRollFrames(
      (import.meta as { env?: Record<string, string> }).env?.[
        VOICE_ACTIVITY_POST_ROLL_MS_ENV
      ],
    ),
    emit: (pcm) => {
      const agc = applyBoundedCaptureAgc(pcm);
      if (agc.gainApplied) this.gainApplied += 1;
      this.maxGain = Math.max(this.maxGain, agc.gain);
      this.clippedSamples += agc.clippedSamples;
      const wav = pcm16ToWav(floatTo16BitPCM(agc.samples), CAPTURE_SAMPLE_RATE);
      this.emitChunk(wav);
    },
  });
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private initializationTimer: ReturnType<typeof setTimeout> | null = null;
  private runtimeWatchdogTimer: ReturnType<typeof setInterval> | null = null;
  private lastAudioCallbackAt: number | null = null;
  private silentInputSinceMs: number | null = null;
  private selectedDeviceExists = false;
  private selectedDeviceIsDefault = false;
  private selectionAuthorized = false;
  private rawFrames = 0;
  private realFrames = 0;
  private voicedFrames = 0;
  private maxRms = 0;
  private maxPeak = 0;
  private chunkProduced = 0;
  private gainApplied = 0;
  private maxGain = 1;
  private clippedSamples = 0;

  getState(): AudioCaptureSnapshot["state"] {
    return this.lifecycle.getSnapshot().state;
  }

  getErrorMessage(): string | null {
    return this.lifecycle.getSnapshot().errorMessage;
  }

  getSnapshot(): AudioCaptureSnapshot {
    return this.lifecycle.getSnapshot();
  }

  getDiagnostics(): AudioCaptureDiagnostics {
    const lifecycle = this.lifecycle.getSnapshot();
    return {
      audioContextState: this.audioCtx?.state ?? "none",
      appTrackLive: this.stream?.getAudioTracks()[0]?.readyState === "live",
      selectedDeviceExists: this.selectedDeviceExists,
      selectedDeviceIsDefault: this.selectedDeviceIsDefault,
      selectionAuthorized: this.selectionAuthorized,
      rawFrames: this.rawFrames,
      realFrames: this.realFrames,
      voicedFrames: this.voicedFrames,
      maxRms: this.maxRms,
      maxPeak: this.maxPeak,
      chunkProduced: this.chunkProduced,
      captureState: lifecycle.state,
      lastErrorCode: lifecycle.lastErrorCode,
      gainApplied: this.gainApplied,
      maxGain: this.maxGain,
      clippedSamples: this.clippedSamples,
      lastAudioCallbackAgeMs:
        this.lastAudioCallbackAt === null
          ? null
          : Math.max(0, Date.now() - this.lastAudioCallbackAt),
    };
  }

  setSelectionDiagnostics(selected: boolean, authorized: boolean): void {
    this.selectedDeviceExists = selected;
    this.selectionAuthorized = authorized;
  }

  onChunk(handler: CaptureChunkHandler): () => void {
    this.chunkHandlers.add(handler);
    return () => this.chunkHandlers.delete(handler);
  }

  onStatus(handler: CaptureStatusHandler): () => void {
    return this.lifecycle.subscribe(handler);
  }

  onNativeUpload(handler: (result: NativeCaptureUploadResult) => void): () => void {
    this.nativeUploadHandlers.add(handler);
    return () => this.nativeUploadHandlers.delete(handler);
  }

  /** One scope exists only for the active microphone lifetime. */
  getCaptureScope(): CaptureScope {
    const deviceId = captureDeviceId();
    if (this.scope.deviceId !== deviceId) {
      this.scope = createCaptureScope(deviceId);
    }
    return this.scope;
  }

  start(): void {
    const generation = this.lifecycle.begin();
    if (generation === null) return;
    this.scope = createCaptureScope(captureDeviceId());
    this.nativeRuntimeRetryAttempts = 0;
    this.rawFrames = 0;
    this.realFrames = 0;
    this.voicedFrames = 0;
    this.maxRms = 0;
    this.maxPeak = 0;
    this.chunkProduced = 0;
    this.gainApplied = 0;
    this.maxGain = 1;
    this.clippedSamples = 0;
    this.lastAudioCallbackAt = null;
    void this.boot(generation);
  }

  stop(): void {
    if (shouldUseNativeAudioRecord()) {
      // A fresh renderer cannot trust its in-memory nativeActive flag.
      void this.nativePlugin.stop().catch(() => undefined);
    }
    // stop 同时覆盖正式 stop 与 pause：在仍有生命周期上下文时先提交已验证
    // 的尾段，再 teardown/reset；standby 的重复 stop 不会重复触发 flush。
    const captureState = this.lifecycle.getSnapshot().state;
    if (captureState === "capturing" || captureState === "initializing") {
      this.voiceChunker.finish();
    }
    this.lifecycle.stop();
    this.nativeRuntimeRetryAttempts = 0;
    this.clearInitializationWatchdog();
    this.clearRuntimeWatchdog();
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.teardown();
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
              const generation = this.lifecycle.getCurrentGeneration();
              if (generation !== null) {
                const retryGeneration = this.failAttempt(
                  generation,
                  `收音身份恢复失败：${detail}`,
                );
                this.teardownNative();
                if (retryGeneration !== null) {
                  this.scheduleRetry(retryGeneration);
                }
              }
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
          const result = normalizeNativeCaptureUpload(event, this.getCaptureScope());
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
      captureSessionId: this.getCaptureScope().captureSessionId,
      correlationSalt: captureCorrelationSessionSalt(),
    });
    window.dispatchEvent(new Event("echodesk:capture-control-refresh"));
  }

  private emitChunk(wav: Blob): void {
    this.chunkProduced += 1;
    for (const h of this.chunkHandlers) h(wav);
  }

  async setFormalMode(meetingId: string | null): Promise<void> {
    if (!shouldUseNativeAudioRecord()) return;
    await this.nativePlugin.setCaptureMode({
      formal: meetingId !== null,
      meetingId: meetingId ?? "",
    });
  }

  private teardown(): void {
    this.clearRuntimeWatchdog();
    this.teardownNative();
    this.proc?.disconnect();
    this.proc = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.audioCtx?.close().catch(() => undefined);
    this.audioCtx = null;
    this.voiceChunker.reset();
    this.silentInputSinceMs = null;
    this.lastAudioCallbackAt = null;
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

  private clearInitializationWatchdog(): void {
    if (this.initializationTimer === null) return;
    clearTimeout(this.initializationTimer);
    this.initializationTimer = null;
  }

  private clearRuntimeWatchdog(): void {
    if (this.runtimeWatchdogTimer === null) return;
    clearInterval(this.runtimeWatchdogTimer);
    this.runtimeWatchdogTimer = null;
  }

  private failWebAudioRuntime(generation: number, message: string): void {
    if (
      !this.isCurrent(generation) ||
      this.lifecycle.getSnapshot().state !== "capturing"
    ) {
      return;
    }
    const retryGeneration = this.failAttempt(generation, message);
    this.teardown();
    if (retryGeneration !== null) this.scheduleRetry(retryGeneration);
  }

  private beginRuntimeWatchdog(generation: number): void {
    this.clearRuntimeWatchdog();
    this.lastAudioCallbackAt = Date.now();
    this.runtimeWatchdogTimer = setInterval(() => {
      if (
        !this.isCurrent(generation) ||
        this.lifecycle.getSnapshot().state !== "capturing"
      ) {
        this.clearRuntimeWatchdog();
        return;
      }
      const track = this.stream?.getAudioTracks()[0];
      const context = this.audioCtx;
      const callbackAge =
        this.lastAudioCallbackAt === null
          ? Number.POSITIVE_INFINITY
          : Date.now() - this.lastAudioCallbackAt;
      if (
        track?.readyState === "live" &&
        context?.state === "running" &&
        callbackAge <= WEB_AUDIO_CALLBACK_STALE_MS
      ) {
        return;
      }
      if (context?.state === "suspended") {
        void context.resume().catch(() => undefined);
      }
      if (callbackAge <= WEB_AUDIO_CALLBACK_STALE_MS && track?.readyState === "live") {
        return;
      }
      this.failWebAudioRuntime(
        generation,
        "麦克风音频流已中断，EchoDesk 正在自动重新连接",
      );
    }, WEB_AUDIO_LIVENESS_CHECK_MS);
  }

  private beginInitializationWatchdog(generation: number): void {
    this.clearInitializationWatchdog();
    this.initializationTimer = setTimeout(() => {
      this.initializationTimer = null;
      if (
        !this.isCurrent(generation) ||
        this.lifecycle.getSnapshot().state !== "initializing"
      ) {
        return;
      }
      const retryGeneration = this.failAttempt(
        generation,
        MIC_INIT_TIMEOUT_MESSAGE,
      );
      this.teardown();
      if (retryGeneration !== null) this.scheduleRetry(retryGeneration);
    }, CAPTURE_INIT_WATCHDOG_MS);
  }

  private publishCapturing(generation: number): boolean {
    if (!this.lifecycle.markCapturing(generation)) return false;
    this.clearInitializationWatchdog();
    return true;
  }

  private failAttempt(
    generation: number,
    errorMessage: string,
  ): number | null {
    const retryGeneration = this.lifecycle.invalidateWithError(
      generation,
      errorMessage,
    );
    if (retryGeneration !== null) this.clearInitializationWatchdog();
    return retryGeneration;
  }

  private scheduleRetry(generation: number): void {
    if (!this.isCurrent(generation)) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.boot(generation);
    }, RETRY_MS);
  }

  private isCurrent(generation: number): boolean {
    return this.lifecycle.isCurrent(generation);
  }

  private observeInputHealth(
    input: Float32Array,
    generation: number,
  ): boolean {
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

    const retryGeneration = this.failAttempt(
      generation,
      "电视麦克风没有有效输入；请确认电视/遥控器麦克风或外接会议麦克风已被系统识别",
    );
    this.teardown();
    if (retryGeneration !== null) this.scheduleRetry(retryGeneration);
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
    const retryGeneration = this.failAttempt(
      generation,
      shouldRetry
        ? message
        : `${message}；自动恢复已达 ${NATIVE_RUNTIME_RETRY_LIMIT} 次上限，请检查麦克风后手动重试`,
    );
    this.teardownNative();
    if (shouldRetry && retryGeneration !== null) {
      this.scheduleRetry(retryGeneration);
    }
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
      if (this.lifecycle.getSnapshot().state !== "capturing") {
        this.publishCapturing(generation);
      }
      return true;
    }

    const now = Date.now();
    this.silentInputSinceMs ??= now;
    this.nativeSilentChunks += 1;
    if (this.lifecycle.getSnapshot().state !== "capturing") {
      this.publishCapturing(generation);
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

  /**
   * Test seam：跳过真实音频积累，直接合成一次 chunk emit。
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
    if (!this.lifecycle.beginRetry(generation)) return;
    this.beginInitializationWatchdog(generation);
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
      this.selectedDeviceExists = selectedTrack?.readyState === "live";
      if (selectedTrack) {
        const selectedSettings = selectedTrack.getSettings();
        this.selectedDeviceIsDefault = selectedSettings.deviceId === "";
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
        selectedTrack.addEventListener("ended", () => {
          this.failWebAudioRuntime(
            generation,
            "麦克风输入已断开，EchoDesk 正在自动重新连接",
          );
        });
        selectedTrack.addEventListener("mute", () => {
          if (this.isCurrent(generation)) {
            console.warn("[audio-capture] input track muted; liveness watchdog active");
          }
        });
      }

      const ctx = isAndroidTvRuntime()
        ? new AudioContext()
        : new AudioContext({ sampleRate: CAPTURE_SAMPLE_RATE });
      this.audioCtx = ctx;
      ctx.addEventListener("statechange", () => {
        if (!this.isCurrent(generation)) return;
        if (ctx.state === "closed") {
          this.failWebAudioRuntime(
            generation,
            "音频上下文已关闭，EchoDesk 正在自动重新连接",
          );
        }
      });
      const src = ctx.createMediaStreamSource(stream);
      // 16 kHz 下每次最多约 64 ms；随后逐帧进入 20 ms VAD。不能再让一整个
      // 多秒窗口成为首次语音上传的前置条件。
      const proc = ctx.createScriptProcessor(WEB_AUDIO_PROCESSOR_BUFFER_SAMPLES, 1, 1);
      this.proc = proc;

      proc.onaudioprocess = (ev) => {
        if (!this.isCurrent(generation)) return;
        this.lastAudioCallbackAt = Date.now();
        const ch = ev.inputBuffer.getChannelData(0);
        this.rawFrames += 1;
        let squareSum = 0;
        let peak = 0;
        for (let i = 0; i < ch.length; i += 1) {
          const absolute = Math.abs(ch[i]);
          squareSum += ch[i] * ch[i];
          peak = Math.max(peak, absolute);
        }
        const rms = Math.sqrt(squareSum / Math.max(1, ch.length));
        this.maxRms = Math.max(this.maxRms, rms);
        this.maxPeak = Math.max(this.maxPeak, peak);
        if (rms > 0.00001 || peak > 0.00005) this.realFrames += 1;
        if (!this.observeInputHealth(ch, generation)) return;
        // 每个高频 WebAudio callback 都立即进入 20 ms VAD；只在有效语音
        // 成段时产生 wav，避免长静音参与 RMS 平均并稀释短句。
        const downsampled = downsample(
          new Float32Array(ch),
          ctx.sampleRate,
          CAPTURE_SAMPLE_RATE,
        );
        for (let offset = 0; offset + 320 <= downsampled.length; offset += 320) {
          let square = 0;
          for (let i = offset; i < offset + 320; i += 1) square += downsampled[i] * downsampled[i];
          if (Math.sqrt(square / 320) >= VOICE_ACTIVITY_FRAME_RMS) this.voicedFrames += 1;
        }
        this.voiceChunker.push(downsampled);
      };
      src.connect(proc);
      proc.connect(ctx.destination);

      if (this.publishCapturing(generation)) {
        this.beginRuntimeWatchdog(generation);
      }
    } catch (e) {
      if (!this.isCurrent(generation)) return;
      const msg = e instanceof Error ? e.message : String(e);
      const retryGeneration = this.failAttempt(generation, msg);
      this.teardown();
      if (retryGeneration !== null) this.scheduleRetry(retryGeneration);
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
        chunkMs: NATIVE_CAPTURE_CHUNK_MS,
      });
      if (!isActiveAttempt()) {
        this.teardownNative();
        await plugin.stop().catch(() => undefined);
        return;
      }
      this.publishCapturing(generation);
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
        this.failAttempt(generation, errorMessage);
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
if (typeof window !== "undefined") {
  (
    window as Window & {
      __echoAudioCapture?: AudioCapture;
      __echoCaptureDiagnostics?: () => AudioCaptureDiagnostics;
    }
  ).__echoCaptureDiagnostics = () => audioCapture.getDiagnostics();
  if (import.meta.env.DEV) {
    (window as Window & { __echoAudioCapture?: AudioCapture }).__echoAudioCapture = audioCapture;
  }
}
