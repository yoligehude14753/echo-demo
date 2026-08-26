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
 * POST。本路由检测 `stt_status==="circuit_open"` 进入熔断态，按
 * BACKOFF_LADDER_MS 退避。真实片段先进入有界 IndexedDB spool，退避期间
 * 保留队首；网络恢复、renderer 重载后继续使用同一 segment/idempotency key。
 *
 * 退避到期后**下一个真实 chunk 自然成为探测**：如果 stt_status 不再是
 * circuit_open → 重置 backoff；如果仍然 circuit_open → 升一级退避并继续
 * 保留新片段到 durable spool。
 */
import {
  CaptureUploadHttpError,
  isDurableCaptureChunkReceipt,
  uploadCaptureChunk,
  type CaptureChunkResponse,
} from "@/api";
import { audioCapture } from "@/capture/audioCapture";
import {
  createCaptureTransportState,
  observeDurableCaptureAcknowledgement,
  type CaptureTransportState,
} from "@/capture/captureOperationalState";
import {
  CaptureUploadPool,
  type CaptureUploadPoolActivity,
} from "@/capture/captureUploadCoordinator";
import {
  CaptureTransportReadinessGate,
} from "@/capture/captureTransportReadiness";
import {
  CAPTURE_SPOOL_GLOBAL_MAX_BYTES,
  CAPTURE_SPOOL_GLOBAL_MAX_ITEMS,
  CAPTURE_SPOOL_MAX_BYTES,
  CAPTURE_SPOOL_MAX_ITEMS,
  CAPTURE_SPOOL_TTL_MS,
  captureUploadRetryDelay,
  type CaptureSpoolLane,
  IndexedDbCaptureUploadSpool,
  captureSpoolPartition,
  isCaptureSpoolPartitionCompatible,
  isCaptureSpoolHardCapacityRejection,
  type CaptureSpoolHardCapacityRejectReason,
  type CaptureSpoolItem,
  type CaptureSpoolSnapshot,
} from "@/capture/captureUploadSpool";
import { CAPTURE_SAMPLE_RATE } from "@/capture/pcm";
import { shouldAttachMeetingOverlay } from "@/domain/session";
import { backendBase } from "@/runtime";
import {
  backendSessionTransportReady,
  currentSessionScopeFingerprint,
  subscribeBackendSessionTransportReadiness,
} from "@/session";
import { useStore } from "@/store";
import {
  isCurrentCaptureAttribution,
  isSameCaptureScope,
} from "@/capture/captureScope";
import { advanceCaptureCircuitWindow } from "@/capture/captureCircuitBackoff";
import { isCaptureSpoolOriginCompatible } from "@/capture/captureSpoolOrigin";
import {
  fenceFormalMeeting,
  formalMeetingPartitions,
  isFormalMeetingFenced,
  registerFormalMeetingPartition,
  releaseFormalMeetingPartitions,
  type FormalMeetingFenceRegistry,
  type FormalMeetingPartitionRegistry,
} from "@/capture/formalMeetingPartitions";

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
  /** 仅在有界 spool / staging 确实无法接纳时上报数据损失。 */
  onChunkDropped?: (reason: "circuit_open" | "backpressure") => void;
}

export interface CaptureTransportDiagnostics {
  transportReady: boolean;
  queueDepth: number;
  globalQueueDepth: number;
  lastEnqueueRejectReason: CaptureSpoolHardCapacityRejectReason | null;
  activePartitionItemCount: number;
  activePartitionByteCount: number;
  activePartitionMaxItems: number;
  activePartitionMaxBytes: number;
  globalItemCount: number;
  globalByteCount: number;
  globalMaxItems: number;
  globalMaxBytes: number;
  partitionCount: number;
  recovering: boolean;
  /** Items fenced to a different origin/principal/device; never sent or counted as backpressure. */
  quarantinedForeignFenceItems: number;
  stagingItems: number;
  stagingBytes: number;
  sent: number;
  /** Chunks accepted by the durable spool in this renderer lifetime. */
  enqueued: number;
  acknowledged: number;
  droppedBackpressure: number;
  expiredItems: number;
  consecutiveFailures: number;
  lastHttpStatus: number | null;
  lastErrorCode: string | null;
  activeInFlightCurrent: number;
  activeInFlightMax: number;
  recoveryInFlightCurrent: number;
  recoveryInFlightMax: number;
  globalInFlightCurrent: number;
  globalInFlightMax: number;
  attemptCount: number;
  acknowledgedCount: number;
  completedRequestCount: number;
  requestLatencyMsSum: number;
  requestLatencyMsMax: number;
}

const FAIL_STREAK_THRESHOLD = 2; // 连续 2 次才报错，避免一次抖动也弹 toast
const CIRCUIT_STREAK_THRESHOLD = 1; // ASR admission/429 已经是明确不可用信号
const BACKPRESSURE_RECOVERY_DEPTH = Math.floor(CAPTURE_SPOOL_MAX_ITEMS / 2);
const captureUploadSpool = new IndexedDbCaptureUploadSpool();
const STARTUP_RECOVERY_POLL_MS = 250;

export interface CaptureStartupRecoveryProjection {
  recovering: boolean;
  queueDepth: number;
}

export interface FormalCaptureQuiescePort {
  awaitRouterEnqueueDrain: () => Promise<void>;
  awaitFormalPartitionIdle: (meetingId: string, timeoutMs: number) => Promise<void>;
  fenceFormalMeeting: (meetingId: string) => void;
  releaseFormalMeeting: (meetingId: string) => void;
}

let activeFormalCaptureQuiescePort: FormalCaptureQuiescePort | null = null;

/** 正式 stop 的产品 capture port：不依赖 window diagnostics。 */
export function stopFormalCaptureProducer(): void {
  audioCapture.stop();
}

export function restoreFormalCaptureProducer(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("echodesk:capture-control-refresh"));
  }
}

export async function awaitFormalCaptureRouterEnqueueDrain(): Promise<void> {
  if (!activeFormalCaptureQuiescePort) {
    throw new Error("formal capture router is not mounted");
  }
  await activeFormalCaptureQuiescePort.awaitRouterEnqueueDrain();
}

export async function awaitFormalCapturePartitionIdle(
  meetingId: string,
  timeoutMs: number,
): Promise<void> {
  if (!activeFormalCaptureQuiescePort) {
    throw new Error("formal capture router is not mounted");
  }
  await activeFormalCaptureQuiescePort.awaitFormalPartitionIdle(meetingId, timeoutMs);
}

export function fenceFormalCaptureMeeting(meetingId: string): void {
  activeFormalCaptureQuiescePort?.fenceFormalMeeting(meetingId);
}

export function releaseFormalCaptureMeeting(meetingId: string): void {
  activeFormalCaptureQuiescePort?.releaseFormalMeeting(meetingId);
}

type CaptureStartupRecoveryHandler = (
  shouldBypass: () => boolean,
) => Promise<CaptureStartupRecoveryProjection>;

let captureStartupRecoveryHandler: CaptureStartupRecoveryHandler | null = null;

export async function prepareCaptureStartupRecovery(
  shouldBypass: () => boolean = () => false,
): Promise<CaptureStartupRecoveryProjection> {
  return captureStartupRecoveryHandler?.(shouldBypass) ?? {
    recovering: false,
    queueDepth: 0,
  };
}

/**
 * 指数退避梯子（毫秒）。每次拿到稳定 circuit_open 升一级，最长 5min。
 * 测试模式（VITE_DIAG_BRAKE_BASE_MS env）会按比例缩短，让 e2e 不用等真 5min。
 */
const DEFAULT_BACKOFF_LADDER_MS = [60_000, 120_000, 300_000, 300_000];

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
  let circuitOpenUntil = 0;
  let backoffLevel = -1;
  let circuitStreak = 0;
  let requestSeq = 0;
  let lastHealthyOrdinal = 0;
  const ladder = backoffLadder();
  let disposed = false;
  let enqueueTail: Promise<void> = Promise.resolve();
  const formalPartitions: FormalMeetingPartitionRegistry = new Map();
  const fencedFormalMeetings: FormalMeetingFenceRegistry = new Set();
  let stagingItems = 0;
  let stagingBytes = 0;
  let quarantinedForeignFenceItems = 0;
  let expiredItems = 0;
  let lastHttpStatus: number | null = null;
  let lastErrorCode: string | null = null;
  let globalQueueDepth = 0;
  let activePartitionItemCount = 0;
  let activePartitionByteCount = 0;
  let globalByteCount = 0;
  let partitionCount = 0;
  let lastEnqueueRejectReason: CaptureSpoolHardCapacityRejectReason | null = null;
  let enqueuedCount = 0;
  let poolActivity: CaptureUploadPoolActivity = {
    activeInFlightCurrent: 0,
    activeInFlightMax: 0,
    recoveryInFlightCurrent: 0,
    recoveryInFlightMax: 0,
    globalInFlightCurrent: 0,
    globalInFlightMax: 0,
    attemptCount: 0,
    acknowledgedCount: 0,
    completedRequestCount: 0,
    requestLatencyMsSum: 0,
    requestLatencyMsMax: 0,
  };
  let transport = createCaptureTransportState(CAPTURE_SPOOL_MAX_ITEMS);
  let backpressureActive = false;
  let readinessGate: CaptureTransportReadinessGate | null = null;

  const emitTransport = (patch: Partial<CaptureTransportState>): void => {
    transport = { ...transport, ...patch };
    handlers?.onTransportStateChange?.({ ...transport });
  };

  const markUploadFailure = (
    error: unknown,
    partition: string | null = coordinator.currentActivePartition(),
    _ordinal?: number,
  ): void => {
    if (partition !== coordinator.currentActivePartition()) return;
    failStreak += 1;
    lastHttpStatus = error instanceof CaptureUploadHttpError ? error.status : null;
    lastErrorCode = error instanceof CaptureUploadHttpError
      ? error.errorClass ?? error.name
      : error instanceof Error
        ? error.name
        : "upload_error";
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
      handlers?.onConnectionLost?.(error);
    }
  };

  const recordBackpressureLoss = (count: number): void => {
    if (count <= 0) return;
    backpressureActive = true;
    emitTransport({
      droppedBackpressure: transport.droppedBackpressure + count,
      warning: transport.warning === "upload_unavailable"
        ? "upload_unavailable"
        : "backpressure",
    });
    handlers?.onChunkDropped?.("backpressure");
  };

  const observeSpoolSnapshot = (snapshot: CaptureSpoolSnapshot): void => {
    globalQueueDepth = snapshot.globalDepth;
    activePartitionItemCount = snapshot.depth;
    activePartitionByteCount = snapshot.bytes;
    globalByteCount = snapshot.globalBytes;
    partitionCount = snapshot.partitionCount;
    const recovered =
      backpressureActive && snapshot.depth <= BACKPRESSURE_RECOVERY_DEPTH;
    if (recovered) backpressureActive = false;
    emitTransport({
      queueDepth: snapshot.depth,
      warning:
        transport.warning === "upload_unavailable"
          ? "upload_unavailable"
          : backpressureActive
            ? "backpressure"
            : "none",
    });
    if (recovered) handlers?.onBackpressureRecovered?.();
  };

  const acknowledgeUpload = (_item: CaptureSpoolItem): void => {
    // CaptureUploadPool may finish a formal partition after the renderer has
    // switched back to its free-capture partition.  That durable receipt is
    // still authoritative transport recovery and must not be discarded.
    if (lostNotified) {
      handlers?.onConnectionRecovered?.();
      lostNotified = false;
    }
    failStreak = 0;
    emitTransport(
      observeDurableCaptureAcknowledgement(transport, {
        backpressureActive,
      }),
    );
    handlers?.onChunkPosted?.();
  };

  const processUploadResult = (
    item: CaptureSpoolItem,
    result: CaptureChunkResponse,
  ): void => {
    const now = Date.now();
    const ordinal = item.ordinal ?? 0;
    const isActivePartition = coordinator.isActivePartition(item.partition);
    if (isActivePartition && result.stt_status === "circuit_open") {
      if (ordinal < lastHealthyOrdinal) {
        return;
      }
      circuitStreak += 1;
      if (circuitStreak < CIRCUIT_STREAK_THRESHOLD) {
        return;
      }
      const advanced = advanceCaptureCircuitWindow(
        { level: backoffLevel, openUntilMs: circuitOpenUntil },
        now,
        ladder,
      );
      if (!advanced.advanced) return;
      backoffLevel = advanced.level;
      circuitOpenUntil = advanced.openUntilMs;
      handlers?.onSttCircuitOpen?.({
        retryAtMs: circuitOpenUntil,
        level: backoffLevel,
      });
      return;
    }
    if (isActivePartition) {
      lastHealthyOrdinal = Math.max(lastHealthyOrdinal, ordinal);
      circuitStreak = 0;
      if (backoffLevel >= 0) {
        backoffLevel = -1;
        circuitOpenUntil = 0;
        handlers?.onSttCircuitClosed?.();
      }
    }
    if (!isCurrentCaptureAttribution(result, audioCapture.getCaptureScope())) return;
    useStore
      .getState()
      .completeTranscriptPartial(result.admission.receipt_id);
    if (result.stt_status === "terminal_ignored") {
      const terminalMeetingId = item.meetingId ?? result.meeting_id ?? null;
      if (terminalMeetingId) {
        fenceFormalMeeting(fencedFormalMeetings, terminalMeetingId);
        const partitions = releaseFormalMeetingPartitions(
          formalPartitions,
          terminalMeetingId,
        );
        for (const partition of partitions) {
          coordinator.cancelPartitionIdle(partition);
        }
        useStore.getState().markMeetingEnded(terminalMeetingId);
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("echodesk:meeting-terminal-capture", {
              detail: terminalMeetingId,
            }),
          );
        }
      }
      return;
    }
    if (result.ambient_stored && result.ambient_text) {
      useStore.getState().addAmbientSegment({
        text: result.ambient_text,
        captured_at: new Date().toISOString(),
        speaker_id: result.speaker_id ?? null,
        speaker_label: result.speaker_label ?? null,
        duration_ms: 0,
        segment_correlation: result.segment_correlation,
      });
    }
    if (result.ambient_stored) handlers?.onAmbientUploaded?.();
    const meetingId = item.meetingId ?? undefined;
    const localMeetingId =
      meetingId !== undefined && result.meeting_id === meetingId
        ? result.meeting_id
        : null;
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
  };

  const resolveSpoolFence = async () => {
    const [base, principalScope] = await Promise.all([
      backendBase(),
      currentSessionScopeFingerprint(),
    ]);
    const deviceId = audioCapture.getCaptureScope().deviceId;
    if (!principalScope) throw new Error("收音身份尚未就绪");
    const origin = base || window.location.origin;
    if (!origin) throw new Error("收音上传目标尚未就绪");
    return { origin, principalScope, deviceId };
  };

  const resolveSpoolTarget = async (
    deviceId: string,
    captureSessionId: string,
    lane: CaptureSpoolLane = "free",
  ) => {
    const { origin, principalScope } = await resolveSpoolFence();
    return {
      origin,
      principalScope,
      partition: captureSpoolPartition(
        origin,
        principalScope,
        deviceId,
        captureSessionId,
        lane,
      ),
    };
  };

  const coordinator: CaptureUploadPool<CaptureChunkResponse> =
    new CaptureUploadPool<CaptureChunkResponse>(
      captureUploadSpool,
      {
      beforeAttempt: (item) => {
        if (readinessGate?.current() !== true) {
          return Date.now() + STARTUP_RECOVERY_POLL_MS;
        }
        return coordinator.isActivePartition(item.partition) &&
          circuitOpenUntil > Date.now()
          ? circuitOpenUntil
          : null;
      },
      retryDelay: captureUploadRetryDelay,
      shouldRetry: (error) =>
        !(error instanceof CaptureUploadHttpError) ||
        error.status === 401 ||
        error.status === 403 ||
        error.status === 408 ||
        error.status === 409 ||
        error.status === 425 ||
        error.status === 429 ||
        error.status >= 500,
      upload: async (item, signal) => {
        const target = await resolveSpoolFence();
        if (
          !isCaptureSpoolOriginCompatible(item.origin, target.origin) ||
          target.principalScope !== item.principalScope ||
          target.deviceId !== item.scope.deviceId ||
          !isCaptureSpoolPartitionCompatible(
            item.partition,
            item.origin,
            item.principalScope,
            item.scope.deviceId,
            item.scope.captureSessionId,
          )
        ) {
          throw new Error("收音上传作用域已切换");
        }
        const captureSessionId =
          item.scope.captureSessionId ?? audioCapture.getCaptureScope().captureSessionId;
        return uploadCaptureChunk(
          item.wav,
          CAPTURE_SAMPLE_RATE,
          item.meetingId ?? undefined,
          {
            signal,
            idempotencyKey: item.idempotencyKey,
            segmentId: item.segmentId,
            capturedAtMs: item.capturedAtMs,
            scope: { ...item.scope, captureSessionId },
            expectedOrigin: target.origin,
          },
        );
      },
      isAcknowledgementValid: (item, result) =>
        isDurableCaptureChunkReceipt(result, {
          ...item.scope,
          captureSessionId:
            item.scope.captureSessionId ?? audioCapture.getCaptureScope().captureSessionId,
        }),
      isPartitionEligible: async (summary) => {
        const target = await resolveSpoolFence();
        const eligible = (
          isCaptureSpoolOriginCompatible(summary.origin, target.origin) &&
          target.principalScope === summary.principalScope &&
          target.deviceId === summary.deviceId &&
          isCaptureSpoolPartitionCompatible(
            summary.partition,
            summary.origin,
            summary.principalScope,
            summary.deviceId,
            summary.captureSessionId,
          )
        );
        if (
          !eligible &&
          !coordinator.isActivePartition(summary.partition)
        ) {
          quarantinedForeignFenceItems += summary.depth;
        }
        return eligible;
      },
      onInventory: (inventory) => {
        // Recompute from the next inventory snapshot; foreign-fence items are
        // deliberately separate from current queue/backpressure metrics.
        quarantinedForeignFenceItems = 0;
        partitionCount = inventory.partitions.length;
        globalQueueDepth = inventory.globalDepth;
        globalByteCount = inventory.globalBytes;
        const activePartition = coordinator.currentActivePartition();
        const active = inventory.partitions.find(
          (summary) => summary.partition === activePartition,
        );
        activePartitionItemCount = active?.depth ?? 0;
        activePartitionByteCount = active?.bytes ?? 0;
      },
      onPoolActivity: (activity) => {
        poolActivity = activity;
        emitTransport({
          inFlight: activity.globalInFlightCurrent > 0,
          sent: activity.attemptCount,
          acknowledged: activity.acknowledgedCount,
        });
      },
      onAttempt: (item) => {
        if (coordinator.isActivePartition(item.partition)) {
          emitTransport({ inFlight: true });
        }
      },
      onAcknowledged: (item, result) => {
        acknowledgeUpload(item);
        processUploadResult(item, result);
      },
      onRetry: (item, _retryCount, error) =>
        markUploadFailure(error, item.partition, item.ordinal),
      onDiscarded: (item, error) =>
        markUploadFailure(error, item.partition, item.ordinal),
      onSnapshot: (snapshot, partition) => {
        if (partition && coordinator.isActivePartition(partition)) {
          observeSpoolSnapshot(snapshot);
        }
      },
      onExpired: (count) => {
        expiredItems += count;
      },
      onError: (error, partition) =>
        markUploadFailure(error, partition ?? null),
      },
    );

  readinessGate = new CaptureTransportReadinessGate(
    {
      current: backendSessionTransportReady,
      subscribe: subscribeBackendSessionTransportReadiness,
    },
    coordinator,
    {
      onReady: () => {
        readinessGate?.kick();
      },
      onNotReady: () => {
        emitTransport({ inFlight: false });
      },
    },
  );

  const quiescePort: FormalCaptureQuiescePort = {
    awaitRouterEnqueueDrain: () => enqueueTail,
    awaitFormalPartitionIdle: async (meetingId, timeoutMs) => {
      const partitions = formalMeetingPartitions(formalPartitions, meetingId);
      if (partitions.length === 0) return;
      await Promise.all(
        partitions.map((partition) =>
          coordinator.awaitPartitionIdle(partition, timeoutMs),
        ),
      );
    },
    fenceFormalMeeting: (meetingId) => {
      fenceFormalMeeting(fencedFormalMeetings, meetingId);
      stopFormalCaptureProducer();
      for (const partition of formalMeetingPartitions(formalPartitions, meetingId)) {
        coordinator.cancelPartitionIdle(partition);
      }
    },
    releaseFormalMeeting: (meetingId) => {
      const partitions = releaseFormalMeetingPartitions(formalPartitions, meetingId);
      for (const partition of partitions) {
        coordinator.cancelPartitionIdle(partition);
      }
    },
  };
  activeFormalCaptureQuiescePort = quiescePort;

  let startupRecoveryTask: Promise<CaptureStartupRecoveryProjection> | null = null;
  const startupRecoveryHandler: CaptureStartupRecoveryHandler = (shouldBypass) => {
    if (startupRecoveryTask) return startupRecoveryTask;
    const task = (async (): Promise<CaptureStartupRecoveryProjection> => {
      // 启动恢复不再用启麦前的 idle scope 伪造 active partition。
      // 历史项只走有界 recovery lane，也不再阻塞 getUserMedia。
      if (
        !disposed &&
        !shouldBypass() &&
        readinessGate?.current() === true
      ) {
        readinessGate.kick();
      }
      emitTransport({ recovering: false });
      return { recovering: false, queueDepth: globalQueueDepth };
    })();
    startupRecoveryTask = task;
    void task
      .finally(() => {
        if (startupRecoveryTask === task) startupRecoveryTask = null;
      })
      .catch(() => undefined);
    return task;
  };
  captureStartupRecoveryHandler = startupRecoveryHandler;

  const frozenMeetingId = (): string | null => {
    const { currentMeetingId, meetings } = useStore.getState();
    if (isFormalMeetingFenced(fencedFormalMeetings, currentMeetingId)) return null;
    const meetingState = currentMeetingId
      ? meetings[currentMeetingId]?.state
      : undefined;
    return shouldAttachMeetingOverlay(
      audioCapture.getState(),
      currentMeetingId,
      meetingState,
    )
      ? currentMeetingId
      : null;
  };

  const persistChunk = async (
    wav: Blob,
    capturedAtMs: number,
    meetingId: string | null,
    scope: ReturnType<typeof audioCapture.getCaptureScope>,
    segmentId: string,
    targetPromise: ReturnType<typeof resolveSpoolTarget>,
  ): Promise<void> => {
    if (meetingId !== null && isFormalMeetingFenced(fencedFormalMeetings, meetingId)) {
      return;
    }
    const target = await targetPromise;
    if (meetingId !== null) {
      registerFormalMeetingPartition(formalPartitions, meetingId, target.partition);
    }
    const result = await captureUploadSpool.enqueue(
      {
        partition: target.partition,
        origin: target.origin,
        principalScope: target.principalScope,
        wav,
        byteSize: wav.size,
        capturedAtMs,
        expiresAtMs: capturedAtMs + CAPTURE_SPOOL_TTL_MS,
        meetingId,
        scope,
        segmentId,
        idempotencyKey: `capture:${segmentId}`,
        retryCount: 0,
        nextAttemptAtMs: capturedAtMs,
      },
      capturedAtMs,
      { activePartition: target.partition },
    );
    if (disposed) return;

    if (result.accepted) enqueuedCount += 1;
    if (!isSameCaptureScope(scope, audioCapture.getCaptureScope())) {
      readinessGate?.kick();
      return;
    }

    // renderer 唯一 active 所有权写入口：durable 事务完成后，还要确认冻结的
    // 麦克风生命周期仍为当前值；readiness/recovery 回调无权取得所有权。
    coordinator.setActivePartition(target.partition);
    observeSpoolSnapshot(result.snapshot);
    if (result.snapshot.expired > 0) expiredItems += result.snapshot.expired;
    if (!result.accepted) {
      if (isCaptureSpoolHardCapacityRejection(result.reason)) {
        lastEnqueueRejectReason = result.reason;
        recordBackpressureLoss(1);
      } else {
        lastEnqueueRejectReason = null;
        const admissionError = new Error("durable capture admission reserved");
        admissionError.name = result.reason;
        markUploadFailure(admissionError);
      }
      return;
    }
    lastEnqueueRejectReason = null;
    readinessGate?.kick();
  };

  const offChunk = audioCapture.onChunk((wav) => {
    if (disposed) return;
    // Admission is durable-first. The serialized tail may grow while the
    // network consumer is retrying, but a small renderer staging threshold
    // must never discard a real chunk before IndexedDB has had a chance to
    // admit it. Only an explicit durable capacity rejection is backpressure.
    const capturedAtMs = Date.now();
    const meetingId = frozenMeetingId();
    const scope = audioCapture.getCaptureScope();
    const segmentId = `${scope.deviceId}:${scope.captureSessionId}:${++requestSeq}`;
    const targetPromise = resolveSpoolTarget(
      scope.deviceId,
      scope.captureSessionId,
      meetingId === null ? "free" : "formal",
    );
    stagingItems += 1;
    stagingBytes += wav.size;
    enqueueTail = enqueueTail
      .then(() =>
        persistChunk(
          wav,
          capturedAtMs,
          meetingId,
          scope,
          segmentId,
          targetPromise,
        ),
      )
      .catch((error: unknown) => {
        if (!disposed && isSameCaptureScope(scope, audioCapture.getCaptureScope())) {
          markUploadFailure(error);
        }
      })
      .finally(() => {
        stagingItems = Math.max(0, stagingItems - 1);
        stagingBytes = Math.max(0, stagingBytes - wav.size);
      });
  });
  const offNativeUpload = audioCapture.onNativeUpload((result) => {
    if (result.ambient_stored && result.ambient_text) {
      useStore.getState().addAmbientSegment({
        text: result.ambient_text,
        captured_at: new Date().toISOString(),
        speaker_id: null,
        speaker_label: null,
        duration_ms: 0,
        segment_correlation: result.segment_correlation,
      });
    }
    if (result.ambient_stored) handlers?.onAmbientUploaded?.();
  });

  handlers?.onTransportStateChange?.({ ...transport });
  const diagnostics = () => ({
    transportReady: readinessGate?.current() === true,
    queueDepth: transport.queueDepth,
    globalQueueDepth,
    lastEnqueueRejectReason,
    activePartitionItemCount,
    activePartitionByteCount,
    activePartitionMaxItems: CAPTURE_SPOOL_MAX_ITEMS,
    activePartitionMaxBytes: CAPTURE_SPOOL_MAX_BYTES,
    globalItemCount: globalQueueDepth,
    globalByteCount,
    globalMaxItems: CAPTURE_SPOOL_GLOBAL_MAX_ITEMS,
    globalMaxBytes: CAPTURE_SPOOL_GLOBAL_MAX_BYTES,
    partitionCount,
    recovering: transport.recovering,
    quarantinedForeignFenceItems,
    stagingItems,
    stagingBytes,
    sent: transport.sent,
    enqueued: enqueuedCount,
    acknowledged: transport.acknowledged,
    droppedBackpressure: transport.droppedBackpressure,
    expiredItems,
    consecutiveFailures: transport.consecutiveFailures,
    lastHttpStatus,
    lastErrorCode,
    activeInFlightCurrent: poolActivity.activeInFlightCurrent,
    activeInFlightMax: poolActivity.activeInFlightMax,
    recoveryInFlightCurrent: poolActivity.recoveryInFlightCurrent,
    recoveryInFlightMax: poolActivity.recoveryInFlightMax,
    globalInFlightCurrent: poolActivity.globalInFlightCurrent,
    globalInFlightMax: poolActivity.globalInFlightMax,
    attemptCount: poolActivity.attemptCount,
    acknowledgedCount: poolActivity.acknowledgedCount,
    completedRequestCount: poolActivity.completedRequestCount,
    requestLatencyMsSum: poolActivity.requestLatencyMsSum,
    requestLatencyMsMax: poolActivity.requestLatencyMsMax,
  });
  if (typeof window !== "undefined") {
    (window as Window & {
      __echoCaptureTransportDiagnostics?: () => CaptureTransportDiagnostics;
    }).__echoCaptureTransportDiagnostics = diagnostics;
  }
  readinessGate.start();

  return () => {
    disposed = true;
    readinessGate?.dispose();
    coordinator.dispose();
    if (captureStartupRecoveryHandler === startupRecoveryHandler) {
      captureStartupRecoveryHandler = null;
    }
    offChunk();
    offNativeUpload();
    if (activeFormalCaptureQuiescePort === quiescePort) {
      activeFormalCaptureQuiescePort = null;
    }
    if (typeof window !== "undefined") {
      delete (window as Window & {
        __echoCaptureTransportDiagnostics?: () => CaptureTransportDiagnostics;
      }).__echoCaptureTransportDiagnostics;
    }
  };
}
