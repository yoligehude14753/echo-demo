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
import { attachCaptureChunkRouter } from "@/capture/captureChunkRouter";
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
  observeCaptureAdmission,
  observeCaptureStatsFailure,
  observeCaptureStatsSuccess,
  type CaptureViewModel,
} from "@/capture/captureOperationalState";
import type {
  CaptureState,
  CaptureStatsSnapshot,
} from "@/domain/session";
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
  type CaptureRuntimeState,
} from "@/capture/freeCaptureMode";

const STATS_POLL_MS = 5_000;
const CONTROL_POLL_MS = 3_000;
const CAPTURE_INIT_WATCHDOG_MS = 18_000;
const CIRCUIT_TOAST_KEY = "stt-circuit-open";
const MIC_INIT_TIMEOUT_MESSAGE =
  "系统录音初始化超时；问答、知识库、联网搜索和文档生成仍可继续使用，请稍后重新打开 EchoDesk 或检查 macOS 麦克风权限。";

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

  const [captureState, setCaptureState] = useState<CaptureState>("standby");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
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

  useEffect(() => installFreeCaptureCommandBridge(), []);

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
    if (!captureEnabled) return;
    if (captureState !== "initializing") return;
    const timer = window.setTimeout(() => {
      setCaptureState((current) => {
        if (current !== "initializing") return current;
        setErrorMessage((currentError) => currentError ?? MIC_INIT_TIMEOUT_MESSAGE);
        message.error({
          content: `麦克风不可用：${MIC_INIT_TIMEOUT_MESSAGE}`,
          key: "mic-init-watchdog",
          duration: 6,
        });
        return "error";
      });
    }, CAPTURE_INIT_WATCHDOG_MS);
    return () => window.clearTimeout(timer);
  }, [captureEnabled, captureState]);

  useEffect(() => {
    if (!captureEnabled) {
      audioCapture.stop();
      setCaptureState("standby");
      setDeviceSelected(false);
      setErrorMessage(null);
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
    previousStatsRef.current = null;
    setAmbientChunks(0);
    setAmbientStored(0);
    setMeetingChunks(0);
    setChunksDroppedCircuit(0);
    setSttCircuitOpenUntil(null);
    const offStatus = audioCapture.onStatus((state, err) => {
      setCaptureState(state);
      setErrorMessage(err ?? null);
      if (state === "error" && err) {
        message.error({
          content: captureErrorNotice(err),
          key: "mic-capture-error",
          duration: 5,
        });
      }
    });

    // 自由模式是持久用户选择：App/会话恢复后的首次权威 control 也必须恢复收音。
    let controlBaseline: number | null = null;
    let activeControl: CaptureControl | null = null;
    const applyControl = async (
      control: CaptureControl,
      allowActivation: boolean,
    ) => {
      if (controlBaseline !== null && control.revision < controlBaseline) return;
      if (
        activeControl &&
        control.revision === activeControl.revision &&
        control.mode === activeControl.mode &&
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
      if (allowActivation && selected) {
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
            audioCapture.start();
            return;
          }
        } catch {
          // 权威授权失败时 fail closed。
        }
        audioCapture.stop();
        setCaptureState("standby");
      } else {
        audioCapture.stop();
        setCaptureState("standby");
        setErrorMessage(null);
      }
    };
    const onControlChange = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      void applyControl(normalizeCaptureControl(detail), true);
    };
    window.addEventListener(CAPTURE_CONTROL_EVENT, onControlChange);
    const fetchControl = async () => {
      try {
        const control = await getCaptureControl({ signal: statsController.signal });
        if (!cancelled && isCurrent(originGeneration)) {
          void applyControl(control, true);
        }
      } catch {
        // 控制 API 暂不可用时保持当前安全状态；绝不因此启麦。
      }
    };
    void fetchControl();
    const controlTimer = window.setInterval(
      () => void fetchControl(),
      CONTROL_POLL_MS,
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

    // 5s 轮询 stats；freshness/admission 各自归约，不能代偿 transport ack。
    let cancelled = false;
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
      offStatus();
      offRouter();
      audioCapture.stop();
    };
  }, [
    backendOriginRevision,
    captureGeneration,
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
    speechDetected: admission.lastGateReason === "ok",
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
