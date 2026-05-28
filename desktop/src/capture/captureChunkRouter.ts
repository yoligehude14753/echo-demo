/**
 * CaptureChunkRouter — 每个 chunk 必走 ambient 主链路；meeting 为可选叠加。
 *
 * POST /capture/chunk
 *   - 永远执行（落盘 + STT + RAG）
 *   - meeting_id 仅当 MeetingSession.in_meeting 时附带
 *
 * M_diag_brake · 优雅止血（reactive backoff）
 * ─────────────────────────────────────────────────────────────────────
 * 用户事故复盘：firered STT 熔断后，前端不知情，继续 8 小时 4495 次徒劳
 * POST。本路由检测响应 `stt_status==="circuit_open"` 进入熔断态，按
 * BACKOFF_LADDER_MS 指数退避，期间 onChunk 直接丢弃（不上传也不缓冲；
 * 缓冲 / 重传留给 v2，因为涉及 disk 队列大小管理）。
 *
 * 退避到期后**下一个真实 chunk 自然成为探测**：如果 stt_status 不再是
 * circuit_open → 重置 backoff；如果仍然 circuit_open → 升一级退避并重新
 * 进入 drop 状态。
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
  /** M_diag_brake：STT 熔断 → 触发指数退避，给 UI 红条 + 倒计时。 */
  onSttCircuitOpen?: (info: { retryAtMs: number; level: number }) => void;
  /** M_diag_brake：熔断退出（探测 chunk 拿到非 circuit_open 响应）。 */
  onSttCircuitClosed?: () => void;
  /** M_diag_brake：因为熔断或暂停态直接丢弃 chunk，让 UI 计数。 */
  onChunkDropped?: (reason: "circuit_open") => void;
}

const FAIL_STREAK_THRESHOLD = 2; // 连续 2 次才报错，避免一次抖动也弹 toast

/**
 * 指数退避梯子（毫秒）。每次拿到 circuit_open 升一级，最长 5min。
 * 测试模式（VITE_DIAG_BRAKE_BASE_MS env）会按比例缩短，让 e2e 不用等真 30s。
 */
const DEFAULT_BACKOFF_LADDER_MS = [30_000, 60_000, 120_000, 300_000];

function backoffLadder(): number[] {
  // 仅 Vite test/dev 环境读 env override；prod 始终走默认梯子
  const raw = (import.meta as { env?: Record<string, string> }).env
    ?.VITE_DIAG_BRAKE_BASE_MS;
  if (!raw) return DEFAULT_BACKOFF_LADDER_MS;
  const base = Number(raw);
  if (!Number.isFinite(base) || base <= 0) return DEFAULT_BACKOFF_LADDER_MS;
  return [base, base * 2, base * 4, base * 10];
}

export function attachCaptureChunkRouter(
  handlers?: CaptureRouterHandlers,
): () => void {
  let failStreak = 0;
  let lostNotified = false;
  // M_diag_brake 熔断状态机
  let circuitOpenUntil = 0; // epoch ms；0 = 未熔断
  let backoffLevel = -1; // -1 = 未熔断；0..N = 第几级退避（用 ladder[level]）
  const ladder = backoffLadder();

  return audioCapture.onChunk(async (wav) => {
    const now = Date.now();
    // ─ 熔断态：直接丢弃 chunk（不上传也不缓冲）─
    // 注意：录音继续，只是不发后端；audioCapture 的 wav buffer 会被 GC
    if (circuitOpenUntil > now) {
      handlers?.onChunkDropped?.("circuit_open");
      return;
    }

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

      // M_diag_brake：熔断检测优先于成功路径
      if (result.stt_status === "circuit_open") {
        // 升一级退避（封顶在 ladder 最大值）
        backoffLevel = Math.min(backoffLevel + 1, ladder.length - 1);
        const retryAfterMs = ladder[backoffLevel];
        circuitOpenUntil = now + retryAfterMs;
        handlers?.onSttCircuitOpen?.({
          retryAtMs: circuitOpenUntil,
          level: backoffLevel,
        });
        // 本 chunk 不计为 lost / posted（语义上属于探测结果）
        return;
      }
      // 探测 chunk 拿到非 circuit_open 响应 → 熔断恢复
      if (backoffLevel >= 0) {
        backoffLevel = -1;
        circuitOpenUntil = 0;
        handlers?.onSttCircuitClosed?.();
      }
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
