/**
 * useEchoCapture — App 根挂载 CaptureSession + CaptureChunkRouter
 */
import { useEffect, useState } from "react";
import { message } from "antd";

import { audioCapture } from "@/capture/audioCapture";
import { attachCaptureChunkRouter } from "@/capture/captureChunkRouter";
import type { CaptureState, CaptureStatus } from "@/domain/session";
import { useStore } from "@/store";

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
        // sticky 持久错误条；恢复时会被替换
        message.error({
          content: `后端连接断开（${msg}），自动重试中…`,
          key: "chunk-upload-error",
          duration: 0,
        });
      },
      onConnectionRecovered: () => {
        // 替换为短 toast 并自动消失
        message.success({
          content: "后端已恢复",
          key: "chunk-upload-error",
          duration: 2,
        });
      },
    });

    return () => {
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
  };
}
