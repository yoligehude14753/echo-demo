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
const CIRCUIT_TOAST_KEY = "stt-circuit-open";
const FALLBACK_TOAST_KEY = "chunk-upload-error";

function formatRetryRemaining(retryAtMs: number): string {
  const remainingS = Math.max(0, Math.round((retryAtMs - Date.now()) / 1000));
  if (remainingS < 60) return `${remainingS} 秒后重试`;
  const m = Math.floor(remainingS / 60);
  const s = remainingS % 60;
  return s > 0 ? `${m} 分 ${s} 秒后重试` : `${m} 分钟后重试`;
}

export function useEchoCapture(): CaptureStatus {
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
    audioCapture.start();

    const offStatus = audioCapture.onStatus((state, err) => {
      setCaptureState(state);
      setErrorMessage(err ?? null);
      if (state === "error" && err) {
        message.error(`麦克风不可用：${err}`);
      }
    });

    const offRouter = attachCaptureChunkRouter({
      onChunkPosted: () => setAmbientChunks((n) => n + 1),
      onAmbientUploaded: () => setAmbientStored((n) => n + 1),
      onMeetingUploaded: () => setMeetingChunks((n) => n + 1),
      onConnectionLost: (e) => {
        const msg = e instanceof Error ? e.message : String(e);
        message.error({
          content: `后端连接断开（${msg}），自动重试中…`,
          key: FALLBACK_TOAST_KEY,
          duration: 0,
        });
      },
      onConnectionRecovered: () => {
        message.success({
          content: "后端已恢复",
          key: FALLBACK_TOAST_KEY,
          duration: 2,
        });
      },
      onSttCircuitOpen: ({ retryAtMs }) => {
        setSttCircuitOpenUntil(retryAtMs);
        message.error({
          content: `语音识别暂时不可用 · 暂停上传 · ${formatRetryRemaining(retryAtMs)}`,
          key: CIRCUIT_TOAST_KEY,
          duration: 0,
        });
      },
      onSttCircuitClosed: () => {
        setSttCircuitOpenUntil(null);
        message.success({
          content: "语音识别已恢复，恢复上传",
          key: CIRCUIT_TOAST_KEY,
          duration: 3,
        });
      },
      onChunkDropped: () => setChunksDroppedCircuit((n) => n + 1),
    });

    // 5s 轮询 7 道门统计。失败静默（不打扰用户；下次重试）。
    let cancelled = false;
    const fetchStats = async () => {
      try {
        const next = await getCaptureStats();
        if (!cancelled) setStats(next);
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
  }, []);

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
