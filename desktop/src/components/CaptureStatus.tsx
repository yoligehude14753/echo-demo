/**
 * CaptureStatus — CaptureSession 状态（纯展示，无控制按钮）
 *
 * 文案设计要点（Phase 4 / 修复"已转 4266 但 0 段入库"误导）：
 *  - "采集"   = 已上传的 chunk 数（含 VAD/底噪/STT 空文本，未入库）
 *  - "入库"   = 真正写入 ambient_segments 表的有效段数
 *  - 两者并列展示，避免用户把"采集量"误读为"转写量"
 *  - 整个 Tag 用 Tooltip 解释差距大的原因（VAD/幻觉门过滤）
 */
import { Tag, Tooltip } from "antd";
import { Loader2 } from "lucide-react";

import type { CaptureStatus as CaptureStatusModel } from "@/domain/session";

interface Props {
  status: CaptureStatusModel;
}

const COUNTER_TOOLTIP =
  "采集 = 已上传的音频块（含静音 / 底噪 / STT 空文本）\n" +
  "入库 = 真正写入 ambient_segments 表的有效转写段\n" +
  "两者差距大属正常：环境音绝大多数是 VAD / 幻觉门过滤掉的噪声";

export default function CaptureStatus({ status }: Props): JSX.Element {
  const {
    state,
    ambientChunks,
    ambientStored,
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

  const ariaLabel = meetingOverlayId
    ? `持续采集中，已采集 ${ambientChunks} 段，入库 ${ambientStored} 段，会议中已上传 ${meetingChunks} 段`
    : `持续采集中，已采集 ${ambientChunks} 段，入库 ${ambientStored} 段（静音/底噪自动过滤）`;

  return (
    <Tooltip title={COUNTER_TOOLTIP} placement="bottomRight">
      <Tag
        color="red"
        className="!m-0"
        data-testid="capture-status"
        aria-label={ariaLabel}
      >
        <span className="inline-flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
          持续采集
          <span className="text-[10px] opacity-80">
            · 采集 {ambientChunks} · 入库 {ambientStored}
          </span>
          {meetingOverlayId ? (
            <span className="text-[10px] opacity-80">
              · 会议中 · 段 {meetingChunks}
            </span>
          ) : (
            <span className="text-[10px] opacity-70">
              · 静音/底噪自动过滤
            </span>
          )}
        </span>
      </Tag>
    </Tooltip>
  );
}
