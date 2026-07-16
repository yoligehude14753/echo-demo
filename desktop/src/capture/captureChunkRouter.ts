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
 * POST。本路由检测连续 `stt_status==="circuit_open"` 才进入熔断态，按
 * BACKOFF_LADDER_MS 短退避，期间 onChunk 直接丢弃（不上传也不缓冲；
 * 缓冲 / 重传留给 v2，因为涉及 disk 队列大小管理）。
 *
 * 退避到期后**下一个真实 chunk 自然成为探测**：如果 stt_status 不再是
 * circuit_open → 重置 backoff；如果仍然 circuit_open → 升一级退避并重新
 * 进入 drop 状态。
 */
import { uploadCaptureChunk } from "@/api";
import { audioCapture } from "@/capture/audioCapture";
import {
  CAPTURE_QUEUE_CAPACITY,
  createCaptureTransportState,
  type CaptureTransportState,
} from "@/capture/captureOperationalState";
import { CAPTURE_SAMPLE_RATE } from "@/capture/pcm";
import { shouldAttachMeetingOverlay } from "@/domain/session";
import {
  BACKEND_ORIGIN_EVENT,
  shouldHideSharedPublicHistory,
} from "@/runtime";
import { useStore } from "@/store";
import { ensureSyncDeviceId } from "@/syncState";

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
  /** transport 轴的本地快照；不写入 session/domain/Hub event。 */
  onTransportStateChange?: (state: CaptureTransportState) => void;
  /** 队列从背压状态排空；仅清除 transport 背压告警。 */
  onBackpressureRecovered?: () => void;
  /** M_diag_brake：STT 熔断 → 触发指数退避，给 UI 红条 + 倒计时。 */
  onSttCircuitOpen?: (info: { retryAtMs: number; level: number }) => void;
  /** M_diag_brake：熔断退出（探测 chunk 拿到非 circuit_open 响应）。 */
  onSttCircuitClosed?: () => void;
  /** M_diag_brake：因为熔断或暂停态直接丢弃 chunk，让 UI 计数。 */
  onChunkDropped?: (reason: "circuit_open" | "backpressure") => void;
}

const FAIL_STREAK_THRESHOLD = 2; // 连续 2 次才报错，避免一次抖动也弹 toast
const CIRCUIT_STREAK_THRESHOLD = 3; // 连续 3 次 circuit_open 才认为 STT 真的不可用
const MAX_PENDING_CHUNKS = CAPTURE_QUEUE_CAPACITY;

/**
 * 指数退避梯子（毫秒）。每次拿到稳定 circuit_open 升一级，最长 30s。
 * 测试模式（VITE_DIAG_BRAKE_BASE_MS env）会按比例缩短，让 e2e 不用等真 30s。
 */
const DEFAULT_BACKOFF_LADDER_MS = [5_000, 10_000, 20_000, 30_000];

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
  let circuitStreak = 0;
  let requestSeq = 0;
  let lastHealthySeq = 0;
  let backendOriginGeneration = 0;
  const ladder = backoffLadder();
  const pending: Blob[] = [];
  let drainingGeneration: number | null = null;
  let disposed = false;
  let activeAbort: AbortController | null = null;
  let transport = createCaptureTransportState(MAX_PENDING_CHUNKS);
  let backpressureActive = false;

  const emitTransport = (
    patch: Partial<CaptureTransportState>,
  ): void => {
    transport = { ...transport, ...patch };
    handlers?.onTransportStateChange?.({ ...transport });
  };

  const acknowledgeUpload = (): void => {
    if (lostNotified) {
      handlers?.onConnectionRecovered?.();
      lostNotified = false;
    }
    failStreak = 0;
    emitTransport({
      inFlight: false,
      acknowledged: transport.acknowledged + 1,
      consecutiveFailures: 0,
      lastSuccessfulUploadAt: Date.now(),
      warning: backpressureActive ? "backpressure" : "none",
    });
    handlers?.onChunkPosted?.();
  };

  const processChunk = async (
    wav: Blob,
    generation: number,
  ): Promise<void> => {
    if (disposed || generation !== backendOriginGeneration) return;
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

    const seq = ++requestSeq;
    const controller = new AbortController();
    activeAbort = controller;
    emitTransport({
      inFlight: true,
      sent: transport.sent + 1,
      queueDepth: pending.length,
    });
    try {
      const result = await uploadCaptureChunk(wav, CAPTURE_SAMPLE_RATE, meetingId, {
        signal: controller.signal,
        idempotencyKey: `capture:${generation}:${seq}`,
        deviceId: ensureSyncDeviceId(),
      });
      if (
        disposed ||
        controller.signal.aborted ||
        generation !== backendOriginGeneration
      ) {
        return;
      }

      // M_diag_brake：熔断检测优先于成功路径
      acknowledgeUpload();
      if (result.stt_status === "circuit_open") {
        if (seq < lastHealthySeq) {
          return;
        }
        circuitStreak += 1;
        failStreak = 0;
        if (circuitStreak < CIRCUIT_STREAK_THRESHOLD) {
          return;
        }
        // 升一级退避（封顶在 ladder 最大值）
        backoffLevel = Math.min(backoffLevel + 1, ladder.length - 1);
        const retryAfterMs = ladder[backoffLevel];
        circuitOpenUntil = now + retryAfterMs;
        handlers?.onSttCircuitOpen?.({
          retryAtMs: circuitOpenUntil,
          level: backoffLevel,
        });
        return;
      }
      lastHealthySeq = Math.max(lastHealthySeq, seq);
      circuitStreak = 0;
      // 探测 chunk 拿到非 circuit_open 响应 → 熔断恢复
      if (backoffLevel >= 0) {
        backoffLevel = -1;
        circuitOpenUntil = 0;
        handlers?.onSttCircuitClosed?.();
      }
      if (result.ambient_stored) {
        if (result.ambient_text) {
          useStore.getState().addAmbientSegment({
            text: result.ambient_text,
            captured_at: new Date().toISOString(),
            speaker_id: result.speaker_id ?? null,
            speaker_label: result.speaker_label ?? null,
            duration_ms: 0,
          });
        }
        handlers?.onAmbientUploaded?.();
      }
      const acceptsLocalMeetingOverlay =
        shouldHideSharedPublicHistory() &&
        meetingId !== undefined &&
        result.meeting_id === meetingId;
      const localMeetingId = acceptsLocalMeetingOverlay ? result.meeting_id : null;
      if (localMeetingId) {
        const store = useStore.getState();
        store.markMeetingActive(localMeetingId, { select: true });
        if (result.meeting_segments.length > 0) {
          store.addMeetingSegments(localMeetingId, result.meeting_segments, {
            select: true,
          });
        }
      }
      if (result.meeting_segments.length > 0) handlers?.onMeetingUploaded?.();
    } catch (e) {
      if (
        disposed ||
        controller.signal.aborted ||
        generation !== backendOriginGeneration
      ) {
        return;
      }
      failStreak += 1;
      emitTransport({
        inFlight: false,
        consecutiveFailures: failStreak,
        warning:
          failStreak >= FAIL_STREAK_THRESHOLD
            ? "upload_unavailable"
            : backpressureActive
              ? "backpressure"
              : transport.warning,
      });
      if (failStreak >= FAIL_STREAK_THRESHOLD && !lostNotified) {
        lostNotified = true;
        handlers?.onConnectionLost?.(e);
      }
    } finally {
      if (activeAbort === controller) activeAbort = null;
    }
  };

  const drain = (): void => {
    const generation = backendOriginGeneration;
    if (drainingGeneration === generation || disposed) return;
    drainingGeneration = generation;
    void (async () => {
      try {
        while (
          !disposed &&
          generation === backendOriginGeneration &&
          pending.length > 0
        ) {
          const next = pending.shift();
          if (next) await processChunk(next, generation);
        }
      } finally {
        if (drainingGeneration === generation) drainingGeneration = null;
        if (!disposed && pending.length === 0 && backpressureActive) {
          backpressureActive = false;
          emitTransport({
            queueDepth: 0,
            warning:
              transport.warning === "backpressure"
                ? "none"
                : transport.warning,
          });
          handlers?.onBackpressureRecovered?.();
        }
        if (!disposed && pending.length > 0) drain();
      }
    })();
  };

  const handleBackendOriginChange = (): void => {
    backendOriginGeneration += 1;
    pending.length = 0;
    activeAbort?.abort();
    activeAbort = null;

    const hadOpenCircuit = backoffLevel >= 0;
    failStreak = 0;
    lostNotified = false;
    circuitOpenUntil = 0;
    backoffLevel = -1;
    circuitStreak = 0;
    requestSeq = 0;
    lastHealthySeq = 0;
    backpressureActive = false;
    transport = createCaptureTransportState(MAX_PENDING_CHUNKS);
    handlers?.onTransportStateChange?.({ ...transport });
    if (hadOpenCircuit) handlers?.onSttCircuitClosed?.();
  };

  window.addEventListener(BACKEND_ORIGIN_EVENT, handleBackendOriginChange);

  const offChunk = audioCapture.onChunk((wav) => {
    if (disposed) return;
    if (pending.length >= MAX_PENDING_CHUNKS) {
      backpressureActive = true;
      emitTransport({
        queueDepth: pending.length,
        droppedBackpressure: transport.droppedBackpressure + 1,
        warning: "backpressure",
      });
      handlers?.onChunkDropped?.("backpressure");
      return;
    }
    pending.push(wav);
    emitTransport({ queueDepth: pending.length });
    drain();
  });

  handlers?.onTransportStateChange?.({ ...transport });

  return () => {
    disposed = true;
    backendOriginGeneration += 1;
    pending.length = 0;
    activeAbort?.abort();
    activeAbort = null;
    window.removeEventListener(BACKEND_ORIGIN_EVENT, handleBackendOriginChange);
    offChunk();
  };
}
