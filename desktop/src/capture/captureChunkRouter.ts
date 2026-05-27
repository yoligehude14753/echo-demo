/**
 * CaptureChunkRouter — 每个 chunk 必走 ambient 主链路；meeting 为可选叠加。
 *
 * POST /capture/chunk
 *   - 永远执行（落盘 + STT + RAG）
 *   - meeting_id 仅当 MeetingSession.in_meeting 时附带
 */
import { uploadCaptureChunk } from "@/api";
import { audioCapture } from "@/capture/audioCapture";
import { CAPTURE_SAMPLE_RATE } from "@/capture/pcm";
import { shouldAttachMeetingOverlay } from "@/domain/session";
import { useStore } from "@/store";

export interface CaptureRouterHandlers {
  /** chunk 已成功 POST（无论是否 stored，VAD 过滤的也算）。 */
  onChunkPosted?: () => void;
  /** chunk 已落库 + STT 出非空文本。 */
  onAmbientUploaded?: () => void;
  onMeetingUploaded?: () => void;
  /** 进入持续失败状态（连续 N 次失败）。 */
  onConnectionLost?: (err: unknown) => void;
  /** 失败后第一次成功 → 连接恢复。 */
  onConnectionRecovered?: () => void;
}

const FAIL_STREAK_THRESHOLD = 2; // 连续 2 次才报错，避免一次抖动也弹 toast

export function attachCaptureChunkRouter(
  handlers?: CaptureRouterHandlers,
): () => void {
  let failStreak = 0;
  let lostNotified = false;

  return audioCapture.onChunk(async (wav) => {
    const { currentMeetingId, meetings } = useStore.getState();
    const meetingState = currentMeetingId
      ? meetings[currentMeetingId]?.state
      : undefined;

    const meetingId = shouldAttachMeetingOverlay(
      audioCapture.getState(),
      currentMeetingId,
      meetingState,
    )
      ? currentMeetingId
      : undefined;

    try {
      const result = await uploadCaptureChunk(wav, CAPTURE_SAMPLE_RATE, meetingId);
      if (lostNotified) {
        handlers?.onConnectionRecovered?.();
        lostNotified = false;
      }
      failStreak = 0;
      handlers?.onChunkPosted?.();
      if (result.ambient_stored) handlers?.onAmbientUploaded?.();
      if (result.meeting_segments.length > 0) handlers?.onMeetingUploaded?.();
    } catch (e) {
      failStreak += 1;
      if (failStreak >= FAIL_STREAK_THRESHOLD && !lostNotified) {
        lostNotified = true;
        handlers?.onConnectionLost?.(e);
      }
    }
  });
}
