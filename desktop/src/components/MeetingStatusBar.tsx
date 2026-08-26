import { useCallback, useEffect, useRef, useState } from "react";
import { Checkbox, Modal, Radio, Tooltip, message } from "antd";
import { Mic, Square } from "lucide-react";
import {
  getCaptureDevices,
  getCurrentMeeting,
  manualEndMeeting,
  manualStartMeeting,
  updateCaptureControl,
} from "@/api";
import {
  announceCaptureControl,
  type CaptureDevice,
  type CaptureMode,
} from "@/capture/captureControl";
import { CaptureControlConflictError } from "@/capture/captureControlConflict";
import { captureDeviceId } from "@/capture/captureDeviceIdentity";
import {
  SESSION_IDENTITY_EVENT,
  currentSessionDeviceId,
  currentSessionIdentityStatus,
  type SessionIdentityStatus,
} from "@/session";
import { type ElectronMicStatus } from "@/runtime";
import { useStore } from "@/store";
import type { EchoEvent, MeetingStateSnapshot } from "@/types";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { requestAndroidCaptureStart } from "@/capture/AndroidCaptureSelector";
import { isNativeMobile } from "@/runtime";
import {
  chooseCaptureDeviceId,
  planFreeCaptureSelection,
  resolveCaptureSetupPolicy,
} from "@/capture/freeCaptureSelection";
import {
  currentCaptureRuntimeSnapshot,
  beginFreeCaptureSetup,
  currentFormalMeetingOverlay,
  currentFreeCaptureSetupSnapshot,
  finishFreeCaptureSetup,
  onFreeCaptureSetupRequest,
  retryFreeCaptureSetup,
  setFormalMeetingOverlay,
  setFreeCaptureEnabled,
  type FreeCaptureSetupSnapshot,
} from "@/capture/freeCaptureMode";
import {
  DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS,
  endFormalMeetingLifecycle,
  isFormalCaptureAttachmentCurrent,
  REQUEST_FORMAL_MEETING_EVENT,
  startFormalMeetingLifecycle,
} from "@/components/meetingStartLifecycle";
import {
  awaitFormalCapturePartitionIdle,
  awaitFormalCaptureRouterEnqueueDrain,
  fenceFormalCaptureMeeting,
  releaseFormalCaptureMeeting,
  restoreFormalCaptureProducer,
  stopFormalCaptureProducer,
} from "@/capture/captureChunkRouter";

/**
 * 全局会议状态条：UI 上唯一控制"是否在开会"的入口。
 *
 * 设计（2026-05 PRD）：
 * - 一个时刻只能有 0 或 1 个会议；状态由后端 MeetingState 单例机决定
 * - 自动检测开/结：后端 detector 触发，通过 WS `meeting.state_changed` 推送
 * - 手动覆盖：用户点击本组件 → manual_start / manual_end
 * - 不展示 meeting_id（用户不关心），只显示「待机 / 会议中（manual）/ 自动记录中（auto）」
 *
 * Auto vs Manual 区分（2026-05 phase4-meeting-deadlock 修复）：
 * - manual：用户主动开始，会议中明确性强 → rose 红 + mm:ss 计时 + Square 图标
 * - auto：环境音被识别为持续对话；计时容易让用户误以为是"正常会议"，
 *   导致顶栏出现"会议中 562:53"这类 9h+ 假象。改为：
 *   amber 暖色 + 文案"自动记录中" + Mic 图标 + 不显示计时
 *   （计时由 hover tooltip 提供"已持续 X 分钟"参考用，不挂主视觉）
 */
function fmtElapsed(startedAt?: string | null): string {
  if (!startedAt) return "";
  const ms = Date.now() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

function elapsedMinutes(startedAt?: string | null): number {
  if (!startedAt) return 0;
  const ms = Date.now() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return 0;
  return Math.floor(ms / 60000);
}

type CaptureAuthority = "backend" | "local";

type CapturePreparation = {
  state:
    | "ready"
    | "selection_required"
    | "permission_required"
    | "identity_failed";
  authority: CaptureAuthority;
};

async function microphoneStatus(): Promise<ElectronMicStatus> {
  try {
    return (await window.echo?.getMicStatus?.()) ?? "unknown";
  } catch {
    return "unknown";
  }
}

export default function MeetingStatusBar(): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const [snap, setSnap] = useState<MeetingStateSnapshot>({
    mode: "idle",
    meeting_id: null,
    started_at: null,
    started_by: null,
  });
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [capturePickerOpen, setCapturePickerOpen] = useState(false);
  const [captureDevices, setCaptureDevices] = useState<CaptureDevice[]>([]);
  const [captureMode, setCaptureMode] = useState<CaptureMode>("single");
  const [selectedDeviceIds, setSelectedDeviceIds] = useState<string[]>([]);
  const [captureRevision, setCaptureRevision] = useState(0);
  const [captureSaving, setCaptureSaving] = useState(false);
  const [captureSelectionStartsFormal, setCaptureSelectionStartsFormal] =
    useState(false);
  const [captureSelectionAuthority, setCaptureSelectionAuthority] =
    useState<CaptureAuthority>("backend");
  // A formal meeting is authoritative as soon as its meeting API succeeds.
  // Capture preparation is deliberately a separate, cancellable follow-up so
  // a slow microphone cannot erase the user's explicit meeting boundary.
  const formalCaptureGenerationRef = useRef(0);
  const events = useStore((s) => s.events);
  const markMeetingActive = useStore((s) => s.markMeetingActive);
  const markMeetingEnded = useStore((s) => s.markMeetingEnded);

  const refresh = useCallback(async () => {
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    try {
      const s = await getCurrentMeeting({ signal: controller.signal });
      if (isCurrent(originGeneration) && !controller.signal.aborted) setSnap(s);
    } catch {
      // 后端不通时静默；CaptureStatus 那里已有错误提示
    } finally {
      unregisterController();
    }
  }, [
    captureGeneration,
    isCurrent,
    registerAbortController,
  ]);

  useEffect(() => {
    setSnap({
      mode: "idle",
      meeting_id: null,
      started_at: null,
      started_by: null,
    });
    setBusy(false);
    void refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [backendOriginRevision, refresh]);

  // 1s 心跳刷新 elapsed
  useEffect(() => {
    if (snap.mode !== "in_meeting") return;
    const t = setInterval(() => setTick((n) => n + 1), 1_000);
    return () => clearInterval(t);
  }, [snap.mode]);

  useEffect(() => {
    setFormalMeetingOverlay(
      snap.mode === "in_meeting" && snap.started_by === "manual"
        ? snap.meeting_id
        : null,
    );
  }, [snap.meeting_id, snap.mode, snap.started_by]);

  // WS 状态变更事件：实时同步
  useEffect(() => {
    if (!events.length) return;
    const recent = events[events.length - 1] as EchoEvent<{
      mode?: string;
      meeting_id?: string;
      started_by?: string;
    }>;
    if (
      recent.type === "meeting.state_changed" ||
      recent.type === "meeting.auto_detected" ||
      recent.type === "meeting.auto_ended" ||
      recent.type === "meeting.ended"
    ) {
      void refresh();
    }
  }, [events, refresh]);

  useEffect(() => {
    const onTerminalCapture = (event: Event) => {
      const meetingId = (event as CustomEvent<string>).detail;
      if (meetingId !== snap.meeting_id) {
        void refresh();
        return;
      }
      setSnap({
        mode: "idle",
        meeting_id: null,
        started_at: null,
        started_by: null,
      });
      setFormalMeetingOverlay(null);
      void refresh();
    };
    window.addEventListener("echodesk:meeting-terminal-capture", onTerminalCapture);
    return () =>
      window.removeEventListener("echodesk:meeting-terminal-capture", onTerminalCapture);
  }, [refresh, snap.meeting_id]);

  const prepareCapture = useCallback(
    async (startFormalAfterSelection: boolean): Promise<CapturePreparation> => {
      const localDeviceId = captureDeviceId();
      const identity = currentSessionIdentityStatus();
      const policy = resolveCaptureSetupPolicy({
        identityPhase: identity.phase,
        identityErrorCode: identity.errorCode,
        micStatus: await microphoneStatus(),
        selected: currentCaptureRuntimeSnapshot()?.selected === true,
      });
      if (policy.kind === "permission_required") {
        message.error("麦克风权限未开启，请先在系统设置中允许 EchoDesk 使用麦克风");
        return {
          state: "permission_required",
          authority: identity.phase === "ready" ? "backend" : "local",
        };
      }
      if (policy.authority === "local") {
        const localControl = {
          mode: "single" as const,
          selectedDeviceIds: [localDeviceId],
          revision: 0,
          authority: "local" as const,
        };
        if (policy.kind === "ready" || !startFormalAfterSelection) {
          announceCaptureControl(localControl);
          setFreeCaptureEnabled(true);
          return { state: "ready", authority: "local" };
        }
        setCaptureDevices([{
          deviceId: localDeviceId,
          deviceName: "本机麦克风",
          platform: "local",
          online: true,
        }]);
        setCaptureRevision(0);
        setCaptureMode("single");
        setSelectedDeviceIds([localDeviceId]);
        setCaptureSelectionAuthority("local");
        setCaptureSelectionStartsFormal(startFormalAfterSelection);
        setCapturePickerOpen(true);
        return { state: "selection_required", authority: "local" };
      }

      const snapshot = await getCaptureDevices();
      const plan = planFreeCaptureSelection(snapshot.devices, {
        sessionDeviceId: currentSessionDeviceId(),
        localDeviceId,
      });
      if (plan.kind === "choose") {
        const onlineDevices = plan.devices;
        let browserDefaultAudioInput = false;
        let browserDefaultTrackDeviceId: string | null = null;
        try {
          const browserDevices = await navigator.mediaDevices?.enumerateDevices?.();
          browserDefaultAudioInput = Boolean(
            browserDevices?.some(
              (device) => device.kind === "audioinput" && device.deviceId === "default",
              ),
          );
          if (!browserDefaultAudioInput) {
            const temporaryStream = await navigator.mediaDevices?.getUserMedia?.({ audio: true });
            const temporaryTrack = temporaryStream?.getAudioTracks?.()[0];
            browserDefaultTrackDeviceId = temporaryTrack?.getSettings?.().deviceId || null;
            temporaryStream?.getTracks?.().forEach((track) => track.stop());
          }
        } catch {
          browserDefaultAudioInput = false;
          browserDefaultTrackDeviceId = null;
        }
        const defaultDeviceId = chooseCaptureDeviceId(plan, {
          browserDefaultAudioInput,
          browserDefaultTrackDeviceId,
        });
        if (!startFormalAfterSelection && defaultDeviceId) {
          // 首次自动恢复不能把后台收音永久卡在 picker。plan 的首个候选
          // 始终是当前已认证 session 的本机设备；正式会议仍要求显式选择。
          const control = await updateCaptureControl({
            mode: "single",
            selectedDeviceIds: [defaultDeviceId],
            expectedRevision: snapshot.control.revision,
          });
          announceCaptureControl(control);
          setFreeCaptureEnabled(true);
          return { state: "ready", authority: "backend" };
        }
        const initialSelection = snapshot.control.selectedDeviceIds.filter((id) =>
          onlineDevices.some((device) => device.deviceId === id),
        );
        setCaptureDevices(onlineDevices);
        setCaptureRevision(snapshot.control.revision);
        setCaptureMode(snapshot.control.mode);
        setSelectedDeviceIds(
          initialSelection.length > 0
            ? initialSelection
            : [
                onlineDevices.some((device) => device.deviceId === localDeviceId)
                  ? localDeviceId
                  : onlineDevices[0].deviceId,
              ],
        );
        setCaptureSelectionAuthority("backend");
        setCaptureSelectionStartsFormal(startFormalAfterSelection);
        setCapturePickerOpen(true);
        return { state: "selection_required", authority: "backend" };
      }
      if (plan.kind === "identity_mismatch") {
        message.error("本机会话身份不可用，未授权收音设备");
        return { state: "identity_failed", authority: "backend" };
      }
      const control = await updateCaptureControl({
        mode: "single",
        selectedDeviceIds: [plan.deviceId],
        expectedRevision: snapshot.control.revision,
      });
      announceCaptureControl(control);
      setFreeCaptureEnabled(true);
      return { state: "ready", authority: "backend" };
    },
    [],
  );

  const runAutomaticSetup = useCallback(async (request: FreeCaptureSetupSnapshot) => {
    if (isNativeMobile() || request.requestId === null) return;
    if (!beginFreeCaptureSetup(request.requestId)) return;
    try {
      const result = await prepareCapture(request.reason === "formal_meeting");
      if (result.state === "ready") {
        finishFreeCaptureSetup(request.requestId, "succeeded");
        message.success("已准备本机自由收音");
      } else if (result.state === "selection_required") {
        finishFreeCaptureSetup(request.requestId, "awaiting_selection");
      } else if (result.state === "permission_required") {
        finishFreeCaptureSetup(
          request.requestId,
          "failed",
          "麦克风权限未开启",
        );
      } else {
        finishFreeCaptureSetup(
          request.requestId,
          "failed",
          "本机会话身份不可用，未授权收音设备",
        );
      }
    } catch (error) {
      console.error("[capture-control] automatic setup failed", error);
      const phase = currentSessionIdentityStatus().phase;
      const terminal = phase === "identity-lost" || phase === "upgrade-required";
      finishFreeCaptureSetup(
        request.requestId,
        terminal ? "failed" : "retryable_failed",
        terminal ? "本机会话身份不可用，未授权收音设备" : "服务连接尚未就绪，等待身份恢复后重试",
      );
      message.error(
        terminal
          ? "本机会话身份不可用，未授权收音设备"
          : "自由收音准备暂未完成，等待服务身份恢复",
      );
    }
  }, [prepareCapture]);

  const prepareCaptureAfterFormalStart = useCallback(async (generation: number) => {
    const stillAttached = () =>
      isFormalCaptureAttachmentCurrent(
        formalCaptureGenerationRef.current,
        generation,
        currentFormalMeetingOverlay(),
      );
    if (!stillAttached()) return;

    const runtime = currentCaptureRuntimeSnapshot();
    const captureAlreadySelected =
      runtime?.freeModeEnabled === true && runtime.selected;
    if (isNativeMobile()) {
      if (!captureAlreadySelected) await requestAndroidCaptureStart();
      return;
    }
    if (captureAlreadySelected || runtime?.selected) {
      setFreeCaptureEnabled(true);
      return;
    }

    // This can request device selection or report a microphone failure. It
    // must never change the meeting lifecycle that was persisted above.
    await prepareCapture(false);
  }, [prepareCapture]);

  useEffect(() => onFreeCaptureSetupRequest((request) => {
    void runAutomaticSetup(request);
  }), [runAutomaticSetup]);

  useEffect(() => {
    const onIdentity = (event: Event) => {
      const detail = (event as CustomEvent<SessionIdentityStatus>).detail;
      if (detail?.phase !== "ready") return;
      const pending = currentFreeCaptureSetupSnapshot();
      if (pending.requestId !== null && retryFreeCaptureSetup(pending.requestId)) {
        // retryFreeCaptureSetup re-delivers to the mounted selector; no timer or
        // polling loop is used, and the coordinator bounds attempts.
      }
    };
    window.addEventListener(SESSION_IDENTITY_EVENT, onIdentity);
    return () => window.removeEventListener(SESSION_IDENTITY_EVENT, onIdentity);
  }, []);

  const onClick = useCallback(async () => {
    if (busy) return;
    const originGeneration = captureGeneration();
    setBusy(true);
    try {
      if (snap.mode === "idle" || snap.started_by === "auto") {
        const started = await startFormalMeetingLifecycle({
          manualStart: manualStartMeeting,
          isCurrent: () => isCurrent(originGeneration),
          nextCaptureGeneration: () => ++formalCaptureGenerationRef.current,
          activate: (s) => {
            setSnap(s);
            setFormalMeetingOverlay(s.meeting_id ?? null);
            if (s.meeting_id) {
              markMeetingActive(s.meeting_id, {
                startedAt: s.started_at,
                select: true,
              });
            }
          },
          prepareCapture: prepareCaptureAfterFormalStart,
          onCaptureError: (error) => {
            console.error("[meeting-status] capture preparation after start failed", error);
          },
        });
        if (started) message.success("已开始会议");
      } else {
        // Invalidate an in-flight capture preparation before clearing the
        // overlay. A late microphone/device callback may start free capture,
        // but it can never attach to this ended meeting.
        const formalMeetingId = snap.meeting_id;
        const ended = await endFormalMeetingLifecycle({
          manualEnd: manualEndMeeting,
          isCurrent: () => isCurrent(originGeneration),
          stopCaptureProducer: stopFormalCaptureProducer,
          awaitCaptureRouterDrain: awaitFormalCaptureRouterEnqueueDrain,
          awaitFormalPartitionIdle: () =>
            awaitFormalCapturePartitionIdle(
              formalMeetingId ?? "",
              DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS,
            ),
          restoreCapture: restoreFormalCaptureProducer,
          canRestoreCapture: async () => {
            if (!formalMeetingId) return false;
            const current = await getCurrentMeeting();
            return (
              current.mode === "in_meeting" &&
              current.meeting_id === formalMeetingId
            );
          },
          onTerminalCapture: () => {
            if (formalMeetingId) {
              fenceFormalCaptureMeeting(formalMeetingId);
              releaseFormalCaptureMeeting(formalMeetingId);
              markMeetingEnded(formalMeetingId);
            }
            setSnap({
              mode: "idle",
              meeting_id: null,
              started_at: null,
              started_by: null,
            });
            setFormalMeetingOverlay(null);
            void refresh();
          },
          invalidateCapture: () => {
            formalCaptureGenerationRef.current += 1;
          },
          deactivate: (s) => {
            setSnap(s);
            setFormalMeetingOverlay(null);
            if (formalMeetingId) {
              fenceFormalCaptureMeeting(formalMeetingId);
              markMeetingEnded(formalMeetingId);
            }
          },
          releaseFormalCapture: () => {
            if (formalMeetingId) releaseFormalCaptureMeeting(formalMeetingId);
          },
          onStopError: () => message.error("会议尾段尚未排空，会议保持进行中，请稍后重试"),
          quiesceTimeoutMs: DEFAULT_FORMAL_QUIESCE_TIMEOUT_MS,
        });
        if (ended) message.success("已结束会议，正在生成纪要…");
      }
    } catch (e) {
      if (!isCurrent(originGeneration)) return;
      console.error("[meeting-status] meeting action failed", e);
      message.error("会议状态更新失败，请重试");
    } finally {
      if (isCurrent(originGeneration)) setBusy(false);
    }
  }, [
    busy,
    captureGeneration,
    isCurrent,
    markMeetingActive,
    markMeetingEnded,
    prepareCaptureAfterFormalStart,
    refresh,
    snap.meeting_id,
    snap.mode,
    snap.started_by,
  ]);

  useEffect(() => {
    const onFormalMeetingRequest = () => {
      void onClick();
    };
    window.addEventListener(REQUEST_FORMAL_MEETING_EVENT, onFormalMeetingRequest);
    return () =>
      window.removeEventListener(
        REQUEST_FORMAL_MEETING_EVENT,
        onFormalMeetingRequest,
      );
  }, [onClick]);

  const confirmCaptureSelection = useCallback(async () => {
    const selected =
      captureMode === "single"
        ? selectedDeviceIds.slice(0, 1)
        : selectedDeviceIds;
    if (selected.length === 0) {
      message.warning("请至少选择一台收音设备");
      return;
    }
    if (!selected.includes(captureDeviceId())) {
      message.warning(
        captureSelectionStartsFormal
          ? "要从本设备开始正式会议，请先把本设备选为收音端"
          : "要在本设备自由收音，请先把本设备选为收音端",
      );
      return;
    }
    setCaptureSaving(true);
    try {
      const control = captureSelectionAuthority === "local"
        ? {
            mode: captureMode,
            selectedDeviceIds: selected,
            revision: captureRevision,
            authority: "local" as const,
          }
        : await updateCaptureControl({
            mode: captureMode,
            selectedDeviceIds: selected,
            expectedRevision: captureRevision,
          });
      announceCaptureControl(control);
      setFreeCaptureEnabled(true);
      setCapturePickerOpen(false);
    } catch (error) {
      console.error("[capture-control] selection failed", error);
      if (error instanceof CaptureControlConflictError) {
        try {
          const refreshed = await getCaptureDevices();
          const onlineDevices = refreshed.devices.filter((device) => device.online);
          const authoritativeSelection = refreshed.control.selectedDeviceIds.filter(
            (id) => onlineDevices.some((device) => device.deviceId === id),
          );
          setCaptureDevices(onlineDevices);
          setCaptureRevision(refreshed.control.revision);
          setCaptureMode(refreshed.control.mode);
          setSelectedDeviceIds(authoritativeSelection);
          message.warning("收音选择已更新，请确认最新选择后重试");
        } catch {
          setCapturePickerOpen(false);
          message.error("无法刷新最新收音选择，请重新打开");
        }
      } else {
        message.error("收音设备选择保存失败，请重试");
      }
    } finally {
      setCaptureSaving(false);
    }
  }, [
    captureMode,
    captureRevision,
    captureSelectionAuthority,
    captureSelectionStartsFormal,
    selectedDeviceIds,
  ]);

  const isMeeting = snap.mode === "in_meeting";
  const isAuto = isMeeting && snap.started_by === "auto";
  const isManual = isMeeting && snap.started_by === "manual";
  void tick; // 强制 elapsed / minutes 重渲染

  const tooltipTitle = !isMeeting
    ? "自由收音持续运行；点击为正式会议建立明确边界"
    : isAuto
      ? `自由模式已识别到对话；点击开始正式会议（已持续 ${elapsedMinutes(snap.started_at)} 分钟）`
      : "点击结束会议（手动开始，将生成纪要）";

  let buttonClass: string;
  if (isManual) {
    buttonClass =
      "bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200";
  } else if (isAuto) {
    buttonClass =
      "bg-amber-50 text-amber-700 hover:bg-amber-100 border border-amber-200";
  } else {
    buttonClass =
      "bg-paper-100 text-ink-700 hover:bg-paper-200 border border-paper-300";
  }

  return (
    <>
    <Modal
      title="选择收音设备"
      open={capturePickerOpen}
      confirmLoading={captureSaving}
      okText={
        captureSelectionStartsFormal
          ? "开启自由收音并开始正式会议"
          : "开启自由收音"
      }
      cancelText="取消"
      onOk={() => void confirmCaptureSelection()}
      onCancel={() => {
        if (!captureSelectionStartsFormal) setFreeCaptureEnabled(false);
        setCapturePickerOpen(false);
      }}
      destroyOnClose
    >
      <Radio.Group
        value={captureMode}
        onChange={(event) => {
          const mode = event.target.value as CaptureMode;
          setCaptureMode(mode);
          if (mode === "single") {
            setSelectedDeviceIds((current) => current.slice(0, 1));
          }
        }}
      >
        <Radio value="single">仅一台设备</Radio>
        <Radio value="multi">多台设备同时收音</Radio>
      </Radio.Group>
      <div className="mt-4 flex flex-col gap-2">
        {captureDevices.map((device) => (
          <Checkbox
            key={device.deviceId}
            checked={selectedDeviceIds.includes(device.deviceId)}
            onChange={(event) => {
              setSelectedDeviceIds((current) => {
                if (captureMode === "single") {
                  return event.target.checked ? [device.deviceId] : [];
                }
                return event.target.checked
                  ? Array.from(new Set([...current, device.deviceId]))
                  : current.filter((id) => id !== device.deviceId);
              });
            }}
          >
            {device.deviceName}
            <span className="ml-2 text-xs text-ink-400">{device.platform}</span>
          </Checkbox>
        ))}
      </div>
    </Modal>
    <Tooltip title={tooltipTitle}>
      <button
        type="button"
        onClick={() => void onClick()}
        disabled={busy}
        className={`app-no-drag inline-flex h-8 min-w-[104px] items-center justify-center gap-1.5 rounded-md px-3 text-[12px] font-semibold transition ${buttonClass} disabled:opacity-50`}
        data-testid="meeting-status-bar"
        aria-label={tooltipTitle}
        aria-pressed={isMeeting}
      >
        {isManual ? (
          <>
            <Square className="w-3 h-3 fill-current" />
            <span>会议中</span>
            <span className="tabular-nums text-[11px] text-rose-600">
              {fmtElapsed(snap.started_at)}
            </span>
          </>
        ) : isAuto ? (
          <>
            <Mic className="w-3 h-3" />
            <span>检测到对话</span>
          </>
        ) : (
          <>
            <Mic className="w-3 h-3" />
            <span>开始正式会议</span>
            <span className="sr-only">自由收音</span>
          </>
        )}
      </button>
    </Tooltip>
    </>
  );
}
