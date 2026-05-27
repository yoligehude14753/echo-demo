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
  onAmbientUploaded?: () => void;
  onMeetingUploaded?: () => void;
  onError?: (err: unknown) => void;
}

export function attachCaptureChunkRouter(
  handlers?: CaptureRouterHandlers,
): () => void {
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
      if (result.ambient_stored) handlers?.onAmbientUploaded?.();
      if (result.meeting_segments.length > 0) handlers?.onMeetingUploaded?.();
    } catch (e) {
      handlers?.onError?.(e);
    }
  });
}
