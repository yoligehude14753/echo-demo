/**
 * useEchoCapture — App 根挂载 CaptureSession + CaptureChunkRouter
 *
 * M_diag_brake：
 * - 5s 轮询 GET /capture/stats，把 7 道门分布暴露给 CaptureStatus Popover
 * - 接收 captureChunkRouter 的 STT 熔断事件 → 持久化红色 toast + 倒计时
 */
import { useEffect, useState } from "react";
import { message } from "antd";

import { getCaptureStats } from "@/api";
import { audioCapture } from "@/capture/audioCapture";
import { attachCaptureChunkRouter } from "@/capture/captureChunkRouter";
import type {
  CaptureState,
  CaptureStatsSnapshot,
  CaptureStatus,
} from "@/domain/session";
import { useStore } from "@/store";

const STATS_POLL_MS = 5_000;
const CAPTURE_INIT_WATCHDOG_MS = 18_000;
const UPLOAD_ERROR_TOAST_DURATION_SECONDS = 8;
const CIRCUIT_TOAST_KEY = "stt-circuit-open";
const FALLBACK_TOAST_KEY = "chunk-upload-error";
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

export function useEchoCapture({ enabled }: EchoCaptureOptions): CaptureStatus {
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meetingState = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId]?.state : undefined,
  );

  const [captureState, setCaptureState] = useState<CaptureState>("initializing");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [ambientChunks, setAmbientChunks] = useState(0);
  const [ambientStored, setAmbientStored] = useState(0);
  const [meetingChunks, setMeetingChunks] = useState(0);
  const [sttCircuitOpenUntil, setSttCircuitOpenUntil] = useState<number | null>(
    null,
  );
  const [chunksDroppedCircuit, setChunksDroppedCircuit] = useState(0);
  const [stats, setStats] = useState<CaptureStatsSnapshot | null>(null);

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
    if (!enabled) return;
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
  }, [captureState, enabled]);

  useEffect(() => {
    if (!enabled) {
      audioCapture.stop();
      setCaptureState("initializing");
      setErrorMessage(null);
      return;
    }
    audioCapture.start();

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

    const offRouter = attachCaptureChunkRouter({
      onChunkPosted: () => setAmbientChunks((n) => n + 1),
      onAmbientUploaded: () => setAmbientStored((n) => n + 1),
      onMeetingUploaded: () => setMeetingChunks((n) => n + 1),
      onConnectionLost: (e) => {
        const msg = e instanceof Error ? e.message : String(e);
        message.error({
          content: `采集上传暂时失败（${msg}），后台会自动重试`,
          key: FALLBACK_TOAST_KEY,
          duration: UPLOAD_ERROR_TOAST_DURATION_SECONDS,
        });
      },
      onConnectionRecovered: () => {
        message.destroy(FALLBACK_TOAST_KEY);
        message.success({
          content: "后端已恢复",
          key: FALLBACK_TOAST_KEY,
          duration: 2,
        });
      },
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
          return;
        }
        message.warning({
          content: "音频上传较慢，已丢弃过期片段以保持实时性",
          key: "capture-backpressure",
          duration: 4,
        });
      },
    });

    // 5s 轮询 7 道门统计。失败静默（不打扰用户；下次重试）。
    let cancelled = false;
    const fetchStats = async () => {
      try {
        const next = await getCaptureStats();
        if (!cancelled) {
          setStats(next);
          message.destroy(FALLBACK_TOAST_KEY);
        }
      } catch {
        // 静默：stats 是诊断辅助，主路径不依赖它
      }
    };
    void fetchStats();
    const statsTimer = window.setInterval(() => void fetchStats(), STATS_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(statsTimer);
      offStatus();
      offRouter();
      audioCapture.stop();
    };
  }, [enabled]);

  const meetingOverlayId =
    captureState === "capturing" &&
    meetingState === "in_meeting" &&
    currentMeetingId
      ? currentMeetingId
      : null;

  return {
    state: captureState,
    ambientChunks,
    ambientStored,
    meetingChunks,
    meetingOverlayId,
    errorMessage,
    sttCircuitOpenUntil,
    chunksDroppedCircuit,
    stats,
  };
}
