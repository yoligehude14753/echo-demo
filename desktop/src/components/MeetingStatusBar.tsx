import { useCallback, useEffect, useState } from "react";
import { Tooltip, message } from "antd";
import { Mic, Square } from "lucide-react";
import {
  getCurrentMeeting,
  manualEndMeeting,
  manualStartMeeting,
} from "@/api";
import { useStore } from "@/store";
import type { EchoEvent, MeetingStateSnapshot } from "@/types";

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

export default function MeetingStatusBar(): JSX.Element {
  const [snap, setSnap] = useState<MeetingStateSnapshot>({
    mode: "idle",
    meeting_id: null,
    started_at: null,
    started_by: null,
  });
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const events = useStore((s) => s.events);

  const refresh = useCallback(async () => {
    try {
      const s = await getCurrentMeeting();
      setSnap(s);
    } catch {
      // 后端不通时静默；CaptureStatus 那里已有错误提示
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [refresh]);

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

  const onClick = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (snap.mode === "idle") {
        const s = await manualStartMeeting();
        setSnap(s);
        message.success("已开始会议");
      } else {
        const s = await manualEndMeeting();
        setSnap(s);
        message.success("已结束会议，正在生成纪要…");
      }
    } catch (e) {
      message.error(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [busy, snap.mode]);

  const isMeeting = snap.mode === "in_meeting";
  const isAuto = isMeeting && snap.started_by === "auto";
  const isManual = isMeeting && snap.started_by === "manual";
  void tick; // 强制 elapsed / minutes 重渲染

  const tooltipTitle = !isMeeting
    ? "点击手动开始会议；未点击时环境音也会持续采集并自动识别会议"
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
    <Tooltip title={tooltipTitle}>
      <button
        type="button"
        onClick={onClick}
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
            <span className="font-mono text-[11px] text-rose-600">
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
            <span>待机</span>
          </>
        )}
      </button>
    </Tooltip>
  );
}
