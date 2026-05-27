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
 * - 不展示 meeting_id（用户不关心），只显示「idle / 会议中（auto/manual）」
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
  void tick; // 强制 elapsed 重渲染

  return (
    <Tooltip
      title={
        isMeeting
          ? `点击结束会议（${snap.started_by === "auto" ? "自动开始" : "手动开始"}，将生成纪要）`
          : "点击手动开始会议（环境音同时持续采集到 RAG）"
      }
    >
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition ${
          isMeeting
            ? "bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200"
            : "bg-paper-200 text-ink-700 hover:bg-paper-300 border border-paper-300"
        } disabled:opacity-50`}
        data-testid="meeting-status-bar"
      >
        {isMeeting ? (
          <>
            <Square className="w-3 h-3 fill-current" />
            <span>会议中</span>
            {snap.started_by === "auto" && (
              <span className="text-[10px] text-rose-500">auto</span>
            )}
            <span className="font-mono text-[11px] text-rose-600">
              {fmtElapsed(snap.started_at)}
            </span>
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
