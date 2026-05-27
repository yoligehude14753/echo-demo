import { useEffect, useRef, useState } from "react";
import { listRecentAmbient, type AmbientSegment } from "@/api";
import { useStore } from "@/store";

const speakerColors = [
  { fg: "#10a37f", bg: "#ecfdf5" },
  { fg: "#2563eb", bg: "#eff6ff" },
  { fg: "#d97706", bg: "#fffbeb" },
  { fg: "#db2777", bg: "#fdf2f8" },
  { fg: "#7c3aed", bg: "#f5f3ff" },
];

function colorForSpeaker(
  label: string | null | undefined,
): { fg: string; bg: string } {
  if (!label) return { fg: "#737373", bg: "#f5f5f5" };
  const idx = parseInt(label.replace(/[^\d]/g, ""), 10) || 0;
  return speakerColors[idx % speakerColors.length];
}

function fmtMs(ms: number): string {
  const s = Math.floor(ms / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function fmtClock(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

/**
 * 转写流主面板：永远显示 ambient 持续转写流（用户期望）。
 *
 * 设计（2026-05 修订）：
 * - ambient 流是 EchoDesk 的核心输出，应**始终可见**，无论是否在开会、是否选了会议
 * - 会议的 segments 是 ambient 的真子集（同一份 STT 复用），所以单独看会议视图是冗余
 * - 选中会议时，把该会议时间窗 [started_at, ended_at] 内的 ambient 高亮即可
 * - 数据：3s 轮询 /capture/recent，加 WS 事件触发立即刷新（会议/ambient 事件来时）
 */
export default function TranscriptStream(): JSX.Element {
  const [segs, setSegs] = useState<AmbientSegment[]>([]);
  const events = useStore((s) => s.events);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  // 滚动容器（自身），不能用 scrollIntoView 否则会顶整个 App body 滚
  const scrollerRef = useRef<HTMLDivElement>(null);
  // 用户是否处在底部附近：未滚到底时不自动追，避免打断阅读
  const stickyToBottomRef = useRef(true);

  // 时间窗：选中会议时高亮该窗内的 segments
  const winStart = meeting?.started_at
    ? new Date(meeting.started_at).getTime()
    : null;
  const winEnd = meeting?.ended_at ? new Date(meeting.ended_at).getTime() : null;

  // 轮询 ambient + WS 事件触发
  useEffect(() => {
    let alive = true;
    const tick = async (): Promise<void> => {
      try {
        const recent = await listRecentAmbient(100);
        if (alive) setSegs(recent);
      } catch {
        // 静默
      }
    };
    void tick();
    const t = setInterval(tick, 3_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // 收到 meeting.segment / state_changed 事件立即刷新
  useEffect(() => {
    if (!events.length) return;
    const last = events[events.length - 1];
    if (
      last.type === "meeting.segment" ||
      last.type === "meeting.auto_detected" ||
      last.type === "meeting.state_changed"
    ) {
      void listRecentAmbient(100)
        .then((r) => setSegs(r))
        .catch(() => undefined);
    }
  }, [events]);

  // 监听滚动：记录用户是否处在底部附近（容差 40px）
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = (): void => {
      const distFromBottom =
        el.scrollHeight - el.clientHeight - el.scrollTop;
      stickyToBottomRef.current = distFromBottom < 40;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [segs.length === 0]);

  // 新片段到达时，仅当用户停在底部附近才自动追，且只在容器内 scrollTop
  useEffect(() => {
    if (!stickyToBottomRef.current) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [segs.length]);

  if (segs.length === 0) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
        <div>等待环境音转写…</div>
        <div className="text-[10px] text-ink-300">
          开口说话即可触发；环境静音/底噪会被自动过滤
        </div>
      </div>
    );
  }

  return (
    <div
      ref={scrollerRef}
      className="flex-1 min-h-0 overflow-y-auto px-8 py-6"
      data-testid="transcript-scroller"
    >
      <div className="max-w-3xl mx-auto space-y-3">
        <div className="text-[11px] text-ink-400 mb-2 px-1 flex items-center gap-2 sticky top-0 bg-paper-50/90 backdrop-blur-sm py-1 z-10">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span>ambient 持续转写 · {segs.length} 条 · 每 3s 刷新</span>
          {meeting && (
            <span className="ml-auto text-[10px] text-ink-500">
              高亮：{meeting.title || currentMeetingId} 时间窗
            </span>
          )}
        </div>
        {segs.map((s, idx) => {
          const c = colorForSpeaker(s.speaker_label);
          const t = new Date(s.captured_at).getTime();
          const inWindow =
            winStart !== null &&
            t >= winStart &&
            (winEnd === null || t <= winEnd);
          return (
            <div
              key={`${s.captured_at}-${idx}`}
              className={`flex gap-3 items-start rounded-md transition px-2 py-1 ${
                inWindow ? "bg-amber-50/50 ring-1 ring-amber-200/60" : ""
              }`}
            >
              <span className="text-[10px] text-ink-400 font-mono shrink-0 pt-1 w-14 text-right">
                {fmtClock(s.captured_at)}
              </span>
              <span
                className="text-[11px] font-medium shrink-0 px-2 py-0.5 rounded-full"
                style={{ color: c.fg, background: c.bg }}
              >
                {s.speaker_label ?? "未识别"}
              </span>
              <span className="text-[14px] text-ink-800 leading-7 flex-1">
                {s.text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// 占位：保留 fmtMs 以备会议详情视图复用
void fmtMs;
