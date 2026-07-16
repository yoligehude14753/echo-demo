import { useCallback, useEffect, useState } from "react";
import { Checkbox, Modal, Radio, Tooltip, message } from "antd";
import { Mic, Square } from "lucide-react";
import {
  endMeeting,
  finalizeMeeting,
  getCaptureDevices,
  getCurrentMeeting,
  manualEndMeeting,
  manualStartMeeting,
  startMeeting,
  updateCaptureControl,
} from "@/api";
import {
  announceCaptureControl,
  type CaptureDevice,
  type CaptureMode,
} from "@/capture/captureControl";
import { ensureSyncDeviceId } from "@/syncState";
import { shouldHideSharedPublicHistory } from "@/runtime";
import { useStore } from "@/store";
import type { EchoEvent, MeetingStateSnapshot } from "@/types";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";

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

function newLocalMeetingId(): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `m-local-${Date.now().toString(36)}-${suffix}`;
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
  const events = useStore((s) => s.events);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const currentMeetingState = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId]?.state : undefined,
  );
  const markMeetingActive = useStore((s) => s.markMeetingActive);
  const markMeetingEnded = useStore((s) => s.markMeetingEnded);
  const upsertMeeting = useStore((s) => s.upsertMeeting);
  const currentMeeting = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId] : undefined,
  );
  const hideSharedPublicHistory = shouldHideSharedPublicHistory();

  const refresh = useCallback(async () => {
    if (hideSharedPublicHistory) return;
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
    hideSharedPublicHistory,
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

  useEffect(() => {
    if (!hideSharedPublicHistory) return;
    if (currentMeetingId && currentMeetingState === "in_meeting") {
      setSnap((prev) => {
        if (prev.mode === "in_meeting" && prev.meeting_id === currentMeetingId) {
          return prev;
        }
        return {
          mode: "in_meeting",
          meeting_id: currentMeetingId,
          started_at: new Date().toISOString(),
          started_by: "manual",
        };
      });
    } else {
      setSnap((prev) =>
        prev.mode === "idle"
          ? prev
          : {
              mode: "idle",
              meeting_id: null,
              started_at: null,
              started_by: null,
            },
      );
    }
  }, [currentMeetingId, currentMeetingState, hideSharedPublicHistory]);

  // 1s 心跳刷新 elapsed
  useEffect(() => {
    if (snap.mode !== "in_meeting") return;
    const t = setInterval(() => setTick((n) => n + 1), 1_000);
    return () => clearInterval(t);
  }, [snap.mode]);

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

  const prepareCapture = useCallback(async (): Promise<boolean> => {
    const snapshot = await getCaptureDevices();
    const onlineDevices = snapshot.devices.filter((device) => device.online);
    const localDeviceId = ensureSyncDeviceId();
    if (onlineDevices.length > 1) {
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
      setCapturePickerOpen(true);
      return false;
    }
    const targetDeviceId = onlineDevices[0]?.deviceId ?? localDeviceId;
    const control = await updateCaptureControl({
      mode: "single",
      selectedDeviceIds: [targetDeviceId],
      expectedRevision: snapshot.control.revision,
    });
    announceCaptureControl(control);
    return true;
  }, []);

  const onClick = useCallback(async (captureReady = false) => {
    if (busy) return;
    const originGeneration = captureGeneration();
    setBusy(true);
    try {
      if (snap.mode === "idle") {
        if (!captureReady && !(await prepareCapture())) return;
        if (hideSharedPublicHistory) {
          const meetingId = newLocalMeetingId();
          await startMeeting(meetingId);
          if (!isCurrent(originGeneration)) return;
          const next: MeetingStateSnapshot = {
            mode: "in_meeting",
            meeting_id: meetingId,
            started_at: new Date().toISOString(),
            started_by: "manual",
          };
          setSnap(next);
          markMeetingActive(meetingId, {
            startedAt: next.started_at,
            select: true,
          });
          message.success("已开始本机会议");
          return;
        }
        const s = await manualStartMeeting();
        if (!isCurrent(originGeneration)) return;
        setSnap(s);
        if (s.meeting_id) {
          markMeetingActive(s.meeting_id, {
            startedAt: s.started_at,
            select: true,
          });
        }
        message.success("已开始会议");
      } else {
        if (hideSharedPublicHistory) {
          const meetingId = snap.meeting_id ?? currentMeetingId;
          if (meetingId) {
            await endMeeting(meetingId);
            if (!isCurrent(originGeneration)) return;
            markMeetingEnded(meetingId);
            upsertMeeting(meetingId, {
              state: "ended",
              minutes_status: "generating",
              minutes_error: null,
            });
            setSnap({
              mode: "idle",
              meeting_id: null,
              started_at: null,
              started_by: null,
            });
            try {
              const minutes = await finalizeMeeting(
                meetingId,
                currentMeeting?.title || "本机会议",
              );
              if (!isCurrent(originGeneration)) return;
              upsertMeeting(meetingId, {
                state: "ended",
                title: minutes.title,
                minutes,
                minutes_status: "ok",
                minutes_error: null,
              });
              message.success("已结束本机会议并生成纪要");
            } catch (e) {
              if (!isCurrent(originGeneration)) return;
              console.error("[meeting-status] public finalize failed", e);
              upsertMeeting(meetingId, {
                state: "ended",
                minutes_status: "generation_failed",
                minutes_error: "纪要生成失败，请重试",
              });
              message.error("会议已结束，但纪要生成失败，请在纪要面板重试");
            }
          }
          return;
        }
        const s = await manualEndMeeting();
        if (!isCurrent(originGeneration)) return;
        setSnap(s);
        if (s.meeting_id) {
          markMeetingEnded(s.meeting_id);
        }
        message.success("已结束会议，正在生成纪要…");
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
    currentMeeting?.title,
    currentMeetingId,
    hideSharedPublicHistory,
    isCurrent,
    markMeetingActive,
    markMeetingEnded,
    prepareCapture,
    snap.meeting_id,
    snap.mode,
    upsertMeeting,
  ]);

  const confirmCaptureSelection = useCallback(async () => {
    const selected =
      captureMode === "single"
        ? selectedDeviceIds.slice(0, 1)
        : selectedDeviceIds;
    if (selected.length === 0) {
      message.warning("请至少选择一台收音设备");
      return;
    }
    setCaptureSaving(true);
    try {
      const control = await updateCaptureControl({
        mode: captureMode,
        selectedDeviceIds: selected,
        expectedRevision: captureRevision,
      });
      announceCaptureControl(control);
      setCapturePickerOpen(false);
      await onClick(true);
    } catch (error) {
      console.error("[capture-control] selection failed", error);
      message.error("收音设备选择已被其它设备更新，请重新选择");
      try {
        const refreshed = await getCaptureDevices();
        setCaptureDevices(refreshed.devices.filter((device) => device.online));
        setCaptureRevision(refreshed.control.revision);
      } catch {
        // 保留当前选择，让用户稍后重试。
      }
    } finally {
      setCaptureSaving(false);
    }
  }, [
    captureMode,
    captureRevision,
    onClick,
    selectedDeviceIds,
  ]);

  const isMeeting = snap.mode === "in_meeting";
  const isAuto = isMeeting && snap.started_by === "auto";
  const isManual = isMeeting && snap.started_by === "manual";
  void tick; // 强制 elapsed / minutes 重渲染

  const tooltipTitle = !isMeeting
    ? "点击开始会议并选择收音设备；未开始时麦克风保持待机"
    : isAuto
      ? `已自动识别为会议并开始记录；点击可主动结束并生成纪要（已持续 ${elapsedMinutes(snap.started_at)} 分钟）`
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
      okText="开始会议"
      cancelText="取消"
      onOk={() => void confirmCaptureSelection()}
      onCancel={() => setCapturePickerOpen(false)}
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
        onClick={() => void onClick(false)}
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
            <span>自动记录中</span>
          </>
        ) : (
          <>
            <Mic className="w-3 h-3" />
            <span>开始会议</span>
            <span className="sr-only">待机</span>
          </>
        )}
      </button>
    </Tooltip>
    </>
  );
}
