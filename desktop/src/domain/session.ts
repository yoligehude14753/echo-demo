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
  /** ambient 主链路已上传 chunk 数 */
  ambientChunks: number;
  /** meeting 叠加层已上传 chunk 数（仅 in_meeting） */
  meetingChunks: number;
  /** 若 meeting 叠加层激活，指向 meeting_id */
  meetingOverlayId: string | null;
  errorMessage: string | null;
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
