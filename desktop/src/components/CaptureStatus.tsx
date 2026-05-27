/**
 * CaptureStatus — CaptureSession 状态（纯展示，无控制按钮）
 */
import { Tag } from "antd";
import { Loader2 } from "lucide-react";

import type { CaptureStatus as CaptureStatusModel } from "@/domain/session";

interface Props {
  status: CaptureStatusModel;
}

export default function CaptureStatus({ status }: Props): JSX.Element {
  const {
    state,
    ambientChunks,
    meetingChunks,
    meetingOverlayId,
    errorMessage,
  } = status;

  if (state === "initializing") {
    return (
      <Tag
        color="blue"
        icon={<Loader2 className="w-3 h-3 animate-spin" />}
        data-testid="capture-status"
      >
        初始化麦克风…
      </Tag>
    );
  }

  if (state === "error") {
    return (
      <Tag color="red" data-testid="capture-status">
        麦克风不可用
        {errorMessage ? ` · ${errorMessage}` : ""} · 5s 后重试
      </Tag>
    );
  }

  return (
    <Tag color="red" className="!m-0" data-testid="capture-status">
      <span className="inline-flex items-center gap-1.5">
        <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
        持续采集
        <span className="text-[10px] opacity-80">· ambient {ambientChunks}</span>
        {meetingOverlayId ? (
          <span className="text-[10px] opacity-80">
            · 会议 {meetingChunks} → {meetingOverlayId}
          </span>
        ) : (
          <span className="text-[10px] opacity-70">
            · 待命（@开始会议 叠加转写）
          </span>
        )}
      </span>
    </Tag>
  );
}
