/**
 * Echo 会话域模型（自上而下，两个正交域）
 *
 * 产品语义（数字分身 / 方案 2）：
 * - CaptureSession 是主链路：App 启动即采集，每个 chunk 必持久化 + STT + RAG（ambient）
 * - MeetingSession 是叠加层：用户 @开始/结束会议 只控制「是否额外写入 meeting pipeline」
 *
 * ┌─────────────────────────────────────────────────────────────┐
 * │ CaptureSession  24h 持续，用户不可手动开关                   │
 * │   initializing → capturing ⇄ error                          │
 * │   每个 chunk → POST /capture/chunk（必走）                    │
 * ├─────────────────────────────────────────────────────────────┤
 * │ MeetingSession   仅 @开始会议 / @结束会议 / @总结会议         │
 * │   idle → in_meeting → ended                                 │
 * │   in_meeting 时 capture chunk 额外带 meeting_id（叠加层）    │
 * └─────────────────────────────────────────────────────────────┘
 */

/** 麦克风采集域 — 与会议无关，永不手动关闭 */
export type CaptureState = "initializing" | "capturing" | "error";

/** 会议域 — 用户手动开关 */
export type MeetingState = "idle" | "in_meeting" | "ended";

export interface CaptureStatus {
  state: CaptureState;
  /**
   * 已上传的 chunk 数（含被 VAD/底噪/STT 空文本过滤掉的、未入库的）。
   * 等于"麦克风产生 + 后端 POST /capture/chunk 200"次数。
   */
  ambientChunks: number;
  /**
   * 真正写入 ambient_segments 表的有效转写段数。
   * 等于 chunk 响应里 `ambient_stored=true` 的次数；通常远小于 ambientChunks。
   */
  ambientStored: number;
  /** meeting 叠加层已上传 chunk 数（仅 in_meeting） */
  meetingChunks: number;
  /** 若 meeting 叠加层激活，指向 meeting_id */
  meetingOverlayId: string | null;
  errorMessage: string | null;
  /**
   * M_diag_brake：STT 熔断退避到期 epoch ms。
   * null = 未熔断；>0 = 当前在熔断态，UI 显示倒计时。
   */
  sttCircuitOpenUntil: number | null;
  /** 熔断态期间被直接 drop（未上传）的 chunk 累计数。 */
  chunksDroppedCircuit: number;
  /**
   * AmbientCapturePipeline 7 道门处理结果计数（后端 GET /capture/stats）。
   * UI Popover 用它显示根因分布；首次加载完成前为 null。
   */
  stats: CaptureStatsSnapshot | null;
}

/** 与 backend/app/use_cases/ambient_capture.py:AmbientStats 字段一致。 */
export interface CaptureStatsSnapshot {
  chunks_total: number;
  gated_rms: number;
  gated_low_speech: number;
  stt_circuit_open: number;
  stt_failed: number;
  stt_empty: number;
  hallu_dropped: number;
  diarize_failed: number;
  /** phase4-diar-deep：diarizer 正常跑但说不出（None）；与 failed 区分用于根因分布。 */
  diarize_returned_none: number;
  stored: number;
  last_chunk_at: string | null;
  last_stored_at: string | null;
}

/** 会议叠加层是否应激活（capture 仍始终上传，只是多带 meeting_id） */
export function shouldAttachMeetingOverlay(
  captureState: CaptureState,
  meetingId: string | null,
  meetingState: MeetingState | undefined,
): meetingId is string {
  return (
    captureState === "capturing" &&
    meetingId !== null &&
    meetingState === "in_meeting"
  );
}
