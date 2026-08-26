/**
 * useEchoCapture — App 根挂载 CaptureSession + CaptureChunkRouter
 *
 * M_diag_brake：
 * - 5s 轮询 GET /capture/stats，把 7 道门分布暴露给 CaptureStatus Popover
 * - 接收 captureChunkRouter 的 STT 熔断事件 → capture-local 状态 + 倒计时
 */
import { useEffect, useRef, useState } from "react";
import { message } from "antd";

import {
  authorizeCaptureControl,
  getCaptureControl,
  getCaptureStats,
  type CaptureStats,
} from "@/api";
import { audioCapture } from "@/capture/audioCapture";
import type { AudioCaptureSnapshot } from "@/capture/audioCaptureState";
import {
  attachCaptureChunkRouter,
  prepareCaptureStartupRecovery,
} from "@/capture/captureChunkRouter";
import {
  CAPTURE_CONTROL_EVENT,
  isDeviceSelected,
  normalizeCaptureControl,
  type CaptureControl,
} from "@/capture/captureControl";
import {
  createCaptureAdmissionState,
  createCaptureFreshnessState,
  createCaptureTransportState,
  hasRecentAcceptedSpeech,
  observeCaptureAdmission,
  observeCaptureStatsFailure,
  observeCaptureStatsSuccess,
  type CaptureViewModel,
} from "@/capture/captureOperationalState";
import type { CaptureStatsSnapshot } from "@/domain/session";
import { useStore } from "@/store";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { captureDeviceId } from "@/capture/captureDeviceIdentity";
import {
  currentFormalMeetingOverlay,
  deriveCaptureRuntimeState,
  installFreeCaptureCommandBridge,
  isFreeCaptureEnabled,
  onFreeCaptureChange,
  publishCaptureRuntime,
  requestFreeCaptureSetup,
  type CaptureRuntimeState,
} from "@/capture/freeCaptureMode";

const STATS_POLL_MS = 5_000;
const CONTROL_POLL_MS = 3_000;
const CIRCUIT_TOAST_KEY = "stt-circuit-open";

function isCaptureLifecycleActive(
  state: AudioCaptureSnapshot["state"],
): boolean {
  return state === "capturing" || state === "initializing";
}

function captureErrorNotice(error: string): string {
  if (/not supported|notsupportederror/i.test(error)) {
    return "当前环境不支持音频采集，请使用 EchoDesk 桌面应用。";
  }
  if (/permission denied|notallowederror|denied/i.test(error)) {
    return "麦克风权限未开启，请在系统设置中允许 EchoDesk 使用麦克风。";
  }
  if (/device not found|notfounderror/i.test(error)) {
    return "未找到可用麦克风，请检查系统输入设备。";
  }
  return "无法使用麦克风，请检查权限和输入设备。";
}

export interface EchoCaptureOptions {
  enabled: boolean;
}

export function useEchoCapture({ enabled }: EchoCaptureOptions): CaptureViewModel {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meetingState = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId]?.state : undefined,
  );

  const [captureSnapshot, setCaptureSnapshot] = useState<AudioCaptureSnapshot>(
    () => audioCapture.getSnapshot(),
  );
  const { state: captureState, errorMessage } = captureSnapshot;
  const [ambientChunks, setAmbientChunks] = useState(0);
  const [ambientStored, setAmbientStored] = useState(0);
  const [meetingChunks, setMeetingChunks] = useState(0);
  const [sttCircuitOpenUntil, setSttCircuitOpenUntil] = useState<number | null>(
    null,
  );
  const [chunksDroppedCircuit, setChunksDroppedCircuit] = useState(0);
  const [stats, setStats] = useState<CaptureStatsSnapshot | null>(null);
  const [transport, setTransport] = useState(createCaptureTransportState);
  const [freshness, setFreshness] = useState(createCaptureFreshnessState);
  const [admission, setAdmission] = useState(createCaptureAdmissionState);
  const [freeModeEnabled, setFreeModeEnabledState] = useState(
    isFreeCaptureEnabled,
  );
  const [deviceSelected, setDeviceSelected] = useState(false);
  const [formalMeetingId, setFormalMeetingId] = useState(
    currentFormalMeetingOverlay,
  );
  const previousStatsRef = useRef<CaptureStats | null>(null);

  useEffect(
    () =>
      onFreeCaptureChange((enabled) => {
        setFreeModeEnabledState(enabled);
        setFormalMeetingId(currentFormalMeetingOverlay());
      }),
    [],
  );

  useEffect(
    () =>
      installFreeCaptureCommandBridge((command) => {
        if (command === "pause") audioCapture.stop();
      }),
    [],
  );

  useEffect(
    () =>
      audioCapture.onStatus((snapshot) => {
        setCaptureSnapshot(snapshot);
        if (snapshot.state === "error" && snapshot.errorMessage) {
          message.error({
            content: captureErrorNotice(snapshot.errorMessage),
            key: "mic-capture-error",
            duration: 5,
          });
        }
      }),
    [],
  );

  useEffect(() => {
    void audioCapture.setFormalMode(formalMeetingId);
  }, [formalMeetingId]);

  const captureEnabled = enabled && freeModeEnabled;

  useEffect(() => {
    if (sttCircuitOpenUntil === null) return;
    const delayMs = Math.max(0, sttCircuitOpenUntil - Date.now());
    const timer = window.setTimeout(() => {
      setSttCircuitOpenUntil((current) => {
        if (current !== sttCircuitOpenUntil) return current;
        message.destroy(CIRCUIT_TOAST_KEY);
        return null;
      });
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [sttCircuitOpenUntil]);

  useEffect(() => {
    if (!enabled) {
      audioCapture.stop();
      setDeviceSelected(false);
      setStats(null);
      setTransport(createCaptureTransportState());
      setFreshness(createCaptureFreshnessState());
      setAdmission(createCaptureAdmissionState());
      previousStatsRef.current = null;
      return;
    }
    const originGeneration = captureGeneration();
    const statsController = new AbortController();
    const unregisterController = registerAbortController(statsController);
    setStats(null);
    setTransport(createCaptureTransportState());
    setFreshness(createCaptureFreshnessState());
    setAdmission(createCaptureAdmissionState());
    setDeviceSelected(false);
    previousStatsRef.current = null;
    setAmbientChunks(0);
    setAmbientStored(0);
    setMeetingChunks(0);
    setChunksDroppedCircuit(0);
    setSttCircuitOpenUntil(null);
    // 自由模式是持久用户选择：App/会话恢复后的首次权威 control 也必须恢复收音。
    let controlBaseline: number | null = null;
    let activeControl: CaptureControl | null = null;
    let setupRequested = false;
    let cancelled = false;
    let startupRecoveryComplete = false;
    const startAuthorizedCapture = async (control: CaptureControl) => {
      // Pause stops the microphone but leaves the durable router mounted so
      // already-admitted items can still drain.
      if (!captureEnabled) {
        audioCapture.stop();
        return;
      }
      if (!startupRecoveryComplete) {
        if (currentFormalMeetingOverlay() === null) {
          await prepareCaptureStartupRecovery(
            () => currentFormalMeetingOverlay() !== null,
          );
        }
        startupRecoveryComplete = true;
      }
      if (
        cancelled ||
        activeControl !== control ||
        !isDeviceSelected(control)
      ) {
        return;
      }
      audioCapture.setSelectionDiagnostics(true, true);
      audioCapture.start();
    };
    const applyControl = async (
      control: CaptureControl,
      allowActivation: boolean,
    ) => {
      if (controlBaseline !== null && control.revision < controlBaseline) return;
      if (
        activeControl &&
        isCaptureLifecycleActive(audioCapture.getState()) &&
        control.revision === activeControl.revision &&
        control.mode === activeControl.mode &&
        (control.authority ?? "backend") ===
          (activeControl.authority ?? "backend") &&
        control.selectedDeviceIds.join("\u0000") ===
          activeControl.selectedDeviceIds.join("\u0000")
      ) {
        return;
      }
      activeControl = control;
      controlBaseline =
        controlBaseline === null
          ? control.revision
          : Math.max(controlBaseline, control.revision);
      const selected = isDeviceSelected(control);
      setDeviceSelected(selected);
      audioCapture.setSelectionDiagnostics(selected, false);
      if (!captureEnabled) {
        audioCapture.stop();
        return;
      }
      if (!selected && !setupRequested) {
        setupRequested = true;
        requestFreeCaptureSetup("first_run");
      }
      if (allowActivation && selected) {
        if (control.authority === "local") {
          // Local authority only starts getUserMedia. Capture upload and cloud
          // state remain fenced by the normal session/transport checks.
          await startAuthorizedCapture(control);
          return;
        }
        try {
          const authorization = await authorizeCaptureControl({
            deviceId: captureDeviceId(),
            revision: control.revision,
          });
          if (
            !cancelled &&
            authorization.allowed &&
            authorization.revision === control.revision
          ) {
            await startAuthorizedCapture(control);
            return;
          }
        } catch {
          // 权威授权失败时 fail closed。
        }
        audioCapture.stop();
        audioCapture.setSelectionDiagnostics(selected, false);
      } else {
        audioCapture.stop();
        audioCapture.setSelectionDiagnostics(selected, false);
      }
    };
    const onControlChange = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      const authority =
        typeof detail === "object" &&
        detail !== null &&
        "authority" in detail &&
        detail.authority === "local"
          ? "local"
          : "backend";
      void applyControl(normalizeCaptureControl(detail, authority), true);
    };
    window.addEventListener(CAPTURE_CONTROL_EVENT, onControlChange);
    const fetchControl = async () => {
      if (!captureEnabled) {
        audioCapture.stop();
        return;
      }
      try {
        const control = await getCaptureControl({ signal: statsController.signal });
        if (!cancelled && isCurrent(originGeneration)) {
          void applyControl(control, true);
        }
      } catch {
        // 控制 API 暂不可用时保持当前安全状态；绝不因此启麦。
      }
    };
    const onNativeControlRefresh = () => void fetchControl();
    window.addEventListener(
      "echodesk:capture-control-refresh",
      onNativeControlRefresh,
    );
    const offRouter = attachCaptureChunkRouter({
      onChunkPosted: () => setAmbientChunks((n) => n + 1),
      onAmbientUploaded: () => setAmbientStored((n) => n + 1),
      onMeetingUploaded: () => setMeetingChunks((n) => n + 1),
      onTransportStateChange: setTransport,
      onSttCircuitOpen: ({ retryAtMs }) => {
        setSttCircuitOpenUntil(retryAtMs);
        message.destroy(CIRCUIT_TOAST_KEY);
      },
      onSttCircuitClosed: () => {
        setSttCircuitOpenUntil(null);
        message.destroy(CIRCUIT_TOAST_KEY);
      },
      onChunkDropped: (reason) => {
        if (reason === "circuit_open") {
          setChunksDroppedCircuit((n) => n + 1);
        }
      },
    });
    void audioCapture.attachNativeRuntime()
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled && isCurrent(originGeneration)) void fetchControl();
      });
    const controlTimer = window.setInterval(
      () => void fetchControl(),
      CONTROL_POLL_MS,
    );

    // 5s 轮询 stats；freshness/admission 各自归约，不能代偿 transport ack。
    let statsRequestSeq = 0;
    const fetchStats = async () => {
      const requestSeq = ++statsRequestSeq;
      try {
        const next = await getCaptureStats({ signal: statsController.signal });
        if (
          !cancelled &&
          isCurrent(originGeneration) &&
          !statsController.signal.aborted &&
          requestSeq === statsRequestSeq
        ) {
          const previous = previousStatsRef.current;
          previousStatsRef.current = next;
          setStats(next);
          setFreshness((current) =>
            observeCaptureStatsSuccess(current, next),
          );
          setAdmission((current) =>
            observeCaptureAdmission(current, previous, next),
          );
        }
      } catch {
        if (
          !cancelled &&
          isCurrent(originGeneration) &&
          !statsController.signal.aborted &&
          requestSeq === statsRequestSeq
        ) {
          setFreshness((current) => observeCaptureStatsFailure(current));
        }
      }
    };
    void fetchStats();
    const statsTimer = window.setInterval(() => void fetchStats(), STATS_POLL_MS);

    return () => {
      cancelled = true;
      statsRequestSeq += 1;
      unregisterController();
      window.clearInterval(statsTimer);
      window.clearInterval(controlTimer);
      window.removeEventListener(CAPTURE_CONTROL_EVENT, onControlChange);
      window.removeEventListener(
        "echodesk:capture-control-refresh",
        onNativeControlRefresh,
      );
      offRouter();
      audioCapture.stop();
    };
  }, [
    backendOriginRevision,
    captureGeneration,
    enabled,
    captureEnabled,
    isCurrent,
    registerAbortController,
  ]);

  const meetingOverlayId =
    captureState === "capturing" &&
    meetingState === "in_meeting" &&
    currentMeetingId
      ? currentMeetingId
      : null;

  const runtimeState: CaptureRuntimeState = deriveCaptureRuntimeState({
    freeModeEnabled,
    selected: deviceSelected,
    captureState,
    formalMeetingId,
    uploadUnavailable: transport.warning === "upload_unavailable",
    speechDetected: hasRecentAcceptedSpeech(admission),
    errorMessage,
  });

  useEffect(() => {
    publishCaptureRuntime({
      version: 1,
      state: runtimeState,
      freeModeEnabled,
      formalMeetingId,
      selected: deviceSelected,
      errorMessage,
    });
  }, [
    deviceSelected,
    errorMessage,
    formalMeetingId,
    freeModeEnabled,
    runtimeState,
  ]);

  return {
    state: captureState,
    runtimeState,
    ambientChunks,
    ambientStored,
    meetingChunks,
    meetingOverlayId,
    errorMessage,
    sttCircuitOpenUntil,
    chunksDroppedCircuit,
    stats,
    transport,
    freshness,
    admission,
  };
}
