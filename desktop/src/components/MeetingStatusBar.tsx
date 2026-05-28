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
 * - 不展示 meeting_id（用户不关心），只显示「待机 / 会议中（manual）/ 持续监听（auto）」
 *
 * Auto vs Manual 区分（2026-05 phase4-meeting-deadlock 修复）：
 * - manual：用户主动开始，会议中明确性强 → rose 红 + mm:ss 计时 + Square 图标
 * - auto：环境音被识别为持续对话；计时容易让用户误以为是"正常会议"，
 *   导致顶栏出现"会议中 562:53"这类 9h+ 假象。改为：
 *   amber 暖色 + 文案"持续监听" + Mic 图标 + 不显示计时
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
  const upsertMeeting = useStore((s) => s.upsertMeeting);
  const currentMeetingIdStore = useStore((s) => s.currentMeetingId);
  const meetingsById = useStore((s) => s.meetings);

  // 用户 2026-05-28：截图状态栏"待机"主面板"会议进行中"。根因：MeetingStatusBar 用
  // 自己的 snap state（轮询 /meetings/current），MinutesView 用 store.meetings.
  // 这两个数据源会脱节（ws 漏发 / replay buffer 不够 / EchoDesk 重启）。
  // 修法：MeetingStatusBar 拿到 mode=idle 时，把 store 里仍标 in_meeting 的
  // currentMeeting 强制改成 ended，让 MinutesView 至少不撒谎。
  const refresh = useCallback(async () => {
    try {
      const s = await getCurrentMeeting();
      setSnap(s);
      // 后端 idle 但 store 仍以为在 meeting → 强制 sync
      if (s.mode === "idle" && currentMeetingIdStore) {
        const m = meetingsById[currentMeetingIdStore];
        if (m && m.state === "in_meeting") {
          upsertMeeting(currentMeetingIdStore, {
            state: "ended",
            ended_at: m.ended_at ?? new Date().toISOString(),
            // 不强改 minutes_status：让真正的 minutes.ready/failed ws 事件决定
          });
        }
      }
    } catch {
      // 后端不通时静默；CaptureStatus 那里已有错误提示
    }
  }, [currentMeetingIdStore, meetingsById, upsertMeeting]);

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
        // 记下结束前的 meeting_id；backend 改 fire-and-forget 后会立刻返回 idle
        const endedId = snap.meeting_id;
        const s = await manualEndMeeting();
        setSnap(s);
        // 后端 fire-and-forget 后 s.mode 已经是 idle；同步 store 让 MinutesView
        // 立刻进入 generating 状态而不是继续"会议进行中"
        if (endedId) {
          upsertMeeting(endedId, {
            state: "ended",
            ended_at: new Date().toISOString(),
            minutes_status: "generating",
          });
        }
        message.success("已结束会议，正在生成纪要…");
      }
    } catch (e) {
      message.error(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [busy, snap.mode, snap.meeting_id, upsertMeeting]);

  const isMeeting = snap.mode === "in_meeting";
  const isAuto = isMeeting && snap.started_by === "auto";
  const isManual = isMeeting && snap.started_by === "manual";
  void tick; // 强制 elapsed / minutes 重渲染

  const tooltipTitle = !isMeeting
    ? "点击手动开始会议（环境音同时持续采集到 RAG）"
    : isAuto
      ? `已自动识别为持续对话，环境音正在归档；点击可主动结束并生成纪要（已持续 ${elapsedMinutes(snap.started_at)} 分钟）`
      : "点击结束会议（手动开始，将生成纪要）";

  let buttonClass: string;
  if (isManual) {
    buttonClass = "bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200";
  } else if (isAuto) {
    buttonClass = "bg-amber-50 text-amber-700 hover:bg-amber-100 border border-amber-200";
  } else {
    buttonClass = "bg-paper-200 text-ink-700 hover:bg-paper-300 border border-paper-300";
  }

  return (
    <Tooltip title={tooltipTitle}>
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition ${buttonClass} disabled:opacity-50`}
        data-testid="meeting-status-bar"
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
            <span>持续监听</span>
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
