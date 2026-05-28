import { useEffect, useMemo, useRef, useState } from "react";
import { listRecentAmbient, type AmbientSegment } from "@/api";
import { useStore } from "@/store";
import type { TranscriptSegment } from "@/types";
import {
  buildSpeakerDisplayMap,
  colorForDisplayIdx,
} from "@/lib/speakerDisplay";

function fmtClockShort(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/**
 * 转写流主面板（v2 气泡布局，参考 Marvis 简化）。
 *
 * 数据源切换（P4 M_meeting_history，2026-05-28）：
 * - currentMeetingId === null（"待机时段"）→ 显示全局 ambient feed（3s 轮询）
 * - currentMeetingId 已选 + 会议 ended/finalized → 显示 meeting.segments（DB 历史）
 * - currentMeetingId 已选 + 会议 in_meeting → 显示 ambient + 时间窗高亮（兼容当前
 *   ambient pipeline：会议中 chunk 既写 ambient_segments 也走 meeting overlay；
 *   两边内容一致但 ambient 更新更快，所以现场仍用 ambient）
 *
 * 视觉规则（来自用户 2026-05-27 反馈）：
 * - 所有"非用户手动输入"的文本（ambient 转写）→ 左侧气泡 + 头像在左
 * - 用户手动输入（未来：CommandBar 标注 self 的回话）→ 右侧气泡 + 头像在右
 *   当前 ambient 没有 is_self 字段 → 全部走左侧 lane，预留右侧给未来
 * - 头像 = 32×32 圆形彩色 + 居中数字（数字就是 remap 后的"说话人 N"序号）
 * - 时间默认隐藏，hover 整条 → 显示 HH:MM（精度到分够用）
 * - 同一说话人连续多条：合并头像（只在第一条显示），间距更紧（4px）；
 *   切换说话人时拉开间距（16px），更易扫读
 */

interface DisplaySegment {
  text: string;
  captured_at: string;
  speaker_label: string | null;
}

function ambientToDisplay(s: AmbientSegment): DisplaySegment {
  return {
    text: s.text,
    captured_at: s.captured_at,
    speaker_label: s.speaker_label,
  };
}

/**
 * 历史会议没存逐 segment 的 wall-clock 时间，只有 start_ms 偏移。
 * 用 meeting.started_at 作为基准 + start_ms 还原近似时间，仅用于 HH:MM 展示
 * 与"同说话人连续合并"判断；与 ambient 的精度差几百 ms 不影响视觉。
 */
function meetingSegmentToDisplay(
  s: TranscriptSegment,
  startedAt: string | undefined,
): DisplaySegment {
  const baseMs = startedAt ? new Date(startedAt).getTime() : Date.now();
  const captured = new Date(baseMs + s.start_ms).toISOString();
  return {
    text: s.text,
    captured_at: captured,
    speaker_label: s.speaker_label ?? null,
  };
}

export default function TranscriptStream(): JSX.Element {
  const [ambient, setAmbient] = useState<AmbientSegment[]>([]);
  const events = useStore((s) => s.events);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickyToBottomRef = useRef(true);

  // 是否走"会议历史"分支：会议已选 + 已结束（ended/finalized 等）+ 有 segments
  // 进行中会议仍走 ambient 分支保持实时性（ambient 是 chunk 写入的最近 100 条）
  const showMeetingHistory =
    currentMeetingId !== null &&
    meeting !== undefined &&
    meeting.state === "ended" &&
    meeting.segments.length > 0;

  const segs: DisplaySegment[] = useMemo(() => {
    if (showMeetingHistory && meeting) {
      return meeting.segments.map((s) =>
        meetingSegmentToDisplay(s, meeting.started_at),
      );
    }
    return ambient.map(ambientToDisplay);
  }, [showMeetingHistory, meeting, ambient]);

  const speakerDisplayMap = useMemo(
    () => buildSpeakerDisplayMap(segs),
    [segs],
  );

  const winStart =
    !showMeetingHistory && meeting?.started_at
      ? new Date(meeting.started_at).getTime()
      : null;
  const winEnd =
    !showMeetingHistory && meeting?.ended_at
      ? new Date(meeting.ended_at).getTime()
      : null;

  // 仅当走 ambient 分支时才轮询；查看历史会议时省网络
  useEffect(() => {
    if (showMeetingHistory) {
      return undefined;
    }
    let alive = true;
    const tick = async (): Promise<void> => {
      try {
        const recent = await listRecentAmbient(100);
        if (alive) setAmbient(recent);
      } catch {
        /* 静默 */
      }
    };
    void tick();
    const t = setInterval(tick, 3_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [showMeetingHistory]);

  useEffect(() => {
    if (showMeetingHistory) return;
    if (!events.length) return;
    const last = events[events.length - 1];
    if (
      last.type === "meeting.segment" ||
      last.type === "meeting.auto_detected" ||
      last.type === "meeting.state_changed"
    ) {
      void listRecentAmbient(100)
        .then((r) => setAmbient(r))
        .catch(() => undefined);
    }
  }, [events, showMeetingHistory]);

  // 提取布尔到变量：eslint react-hooks/exhaustive-deps 不支持复合表达式作为 dep
  const hasNoSegments = segs.length === 0;
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = (): void => {
      const distFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
      stickyToBottomRef.current = distFromBottom < 40;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [hasNoSegments]);

  useEffect(() => {
    if (!stickyToBottomRef.current) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [segs.length]);

  if (segs.length === 0) {
    if (showMeetingHistory) {
      // 历史会议但 segments 为空：理论上不会走到（hook 触发了 fetch 才标 loaded）
      return (
        <div className="flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
          <div>该会议未保存逐字稿</div>
          <div className="text-[10px] text-ink-300">
            可能 STT 服务在该会议期间不可用
          </div>
        </div>
      );
    }
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
        <div>等待环境音转写…</div>
        <div className="text-[10px] text-ink-300">
          开口说话即可触发；环境静音/底噪会被自动过滤
        </div>
      </div>
    );
  }

  const headerLine = showMeetingHistory
    ? `历史会议 · ${meeting?.title || currentMeetingId} · ${segs.length} 段`
    : `ambient 持续转写 · ${segs.length} 条 · 每 3s 刷新`;
  const headerDot = showMeetingHistory
    ? "bg-ink-400"
    : "bg-emerald-500 animate-pulse";

  return (
    <div
      ref={scrollerRef}
      className="flex-1 min-h-0 overflow-y-auto px-6 py-4"
      data-testid="transcript-scroller"
      data-mode={showMeetingHistory ? "meeting-history" : "ambient"}
    >
      <div className="max-w-3xl mx-auto">
        <div className="text-[11px] text-ink-400 mb-3 px-1 flex items-center gap-2 sticky top-0 bg-paper-50/90 backdrop-blur-sm py-1 z-10">
          <span className={`w-1.5 h-1.5 rounded-full ${headerDot}`} />
          <span>{headerLine}</span>
          {!showMeetingHistory && meeting && (
            <span className="ml-auto text-[10px] text-ink-500">
              高亮：{meeting.title || currentMeetingId} 时间窗
            </span>
          )}
        </div>
        {segs.map((s, idx) => {
          const displayIdx = s.speaker_label
            ? (speakerDisplayMap.get(s.speaker_label) ?? 0)
            : 0;
          const c = colorForDisplayIdx(displayIdx);
          const t = new Date(s.captured_at).getTime();
          const inWindow =
            winStart !== null &&
            t >= winStart &&
            (winEnd === null || t <= winEnd);

          // 当前后端没有标 self 的字段 → 全部 ambient 一律视作"他人"靠左
          // 未来若 segment.speaker_id === 用户自己的 voiceprint，把这里改成 true
          const isSelf = false;

          // 连续同说话人合并：只在第一条显示头像，气泡间距收紧
          const prev = idx > 0 ? segs[idx - 1] : null;
          const sameSpeakerAsPrev =
            prev !== null && prev.speaker_label === s.speaker_label;

          const containerSpacing = sameSpeakerAsPrev ? "mt-1" : "mt-4";
          const displayLabel =
            displayIdx > 0 ? `说话人 ${displayIdx}` : "未识别";

          // 头像：32px 圆形，背景柔色 + 同色边框，居中数字
          const avatar = (
            <div
              className="shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-[12px] font-semibold select-none"
              style={{
                color: c.fg,
                background: c.bg,
                boxShadow: `inset 0 0 0 1px ${c.ring}`,
              }}
              title={
                s.speaker_label
                  ? `${displayLabel}（全局编号 ${s.speaker_label}）`
                  : "未识别说话人"
              }
              data-testid="speaker-avatar"
            >
              {displayIdx > 0 ? displayIdx : "?"}
            </div>
          );
          const avatarSpacer = (
            <div className="shrink-0 w-8 h-8" aria-hidden="true" />
          );

          return (
            <div
              key={`${s.captured_at}-${idx}`}
              className={`group flex gap-2 items-end ${containerSpacing} ${
                isSelf ? "flex-row-reverse" : "flex-row"
              }`}
              data-testid="transcript-row"
            >
              {sameSpeakerAsPrev ? avatarSpacer : avatar}
              <div
                className={`flex flex-col min-w-0 max-w-[78%] ${
                  isSelf ? "items-end" : "items-start"
                }`}
              >
                {!sameSpeakerAsPrev && (
                  <div
                    className={`text-[11px] mb-0.5 px-1 ${
                      isSelf ? "text-right" : "text-left"
                    }`}
                    style={{ color: c.fg }}
                    data-testid="speaker-tag"
                  >
                    {displayLabel}
                  </div>
                )}
                <div
                  className={`relative text-[14px] leading-6 px-3.5 py-2 rounded-2xl shadow-sm border break-words ${
                    inWindow
                      ? "border-amber-300/70 ring-1 ring-amber-200/60"
                      : "border-paper-300"
                  } ${
                    isSelf
                      ? "bg-blue-500 text-white border-blue-500"
                      : "bg-white text-ink-800"
                  }`}
                >
                  {s.text}
                  {/* hover 时显示时间（仅 HH:MM） */}
                  <span
                    className={`absolute top-1/2 -translate-y-1/2 text-[10px] text-ink-400 font-mono opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap select-none ${
                      isSelf ? "right-full mr-2" : "left-full ml-2"
                    }`}
                    data-testid="transcript-time"
                  >
                    {fmtClockShort(s.captured_at)}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
