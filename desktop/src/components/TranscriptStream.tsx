import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { listRecentAmbient, type AmbientSegment } from "@/api";
import { useStore, type ConversationEvent } from "@/store";
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
  /**
   * 用户 2026-05-28 反馈：CommandBar 输入要进转写流（右），Echo / RAG 回复
   * 要在转写流（左）。同源合并：所有 conversation events 转成 DisplaySegment，
   * convKind 决定渲染样式：
   *   - user_command → 右侧 + 紫色 "我" 头像
   *   - assistant_reply → 左侧 + Echo "E" 头像 + 高亮气泡
   *   - rag_answer → 左侧 + Echo "E" 头像 + 引用列表
   * undefined → STT 真实 segment（保持原路径）
   */
  convKind?: ConversationEvent["kind"];
  convStatus?: ConversationEvent["status"];
  convCitations?: ConversationEvent["citations"];
  convId?: string;
}

function convToDisplay(ev: ConversationEvent): DisplaySegment {
  return {
    text: ev.text,
    captured_at: ev.ts,
    speaker_label: null,
    convKind: ev.kind,
    convStatus: ev.status,
    convCitations: ev.citations,
    convId: ev.id,
  };
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

  const conversationEvents = useStore((s) => s.conversationEvents);

  // 合并 STT segments + 人机对话事件，按 ts 升序排
  const segs: DisplaySegment[] = useMemo(() => {
    const base: DisplaySegment[] =
      showMeetingHistory && meeting
        ? meeting.segments.map((s) =>
            meetingSegmentToDisplay(s, meeting.started_at),
          )
        : ambient.map(ambientToDisplay);
    if (conversationEvents.length === 0) return base;
    const convs = conversationEvents.map(convToDisplay);
    const merged = [...base, ...convs];
    merged.sort((a, b) =>
      new Date(a.captured_at).getTime() - new Date(b.captured_at).getTime(),
    );
    return merged;
  }, [showMeetingHistory, meeting, ambient, conversationEvents]);

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

  return (
    <div
      ref={scrollerRef}
      className="flex-1 min-h-0 overflow-y-auto px-6 py-4"
      data-testid="transcript-scroller"
      data-mode={showMeetingHistory ? "meeting-history" : "ambient"}
    >
      <div className="max-w-3xl mx-auto">
        {segs.map((s, idx) => {
          const isUserCmd = s.convKind === "user_command";
          const isEchoReply =
            s.convKind === "assistant_reply" || s.convKind === "rag_answer";
          const isConv = isUserCmd || isEchoReply;

          const displayIdx = !isConv && s.speaker_label
            ? (speakerDisplayMap.get(s.speaker_label) ?? 0)
            : 0;
          const c = isConv
            // 人机对话用专属配色：用户=蓝、Echo=紫
            ? isUserCmd
              ? { fg: "#1d4ed8", bg: "#dbeafe", ring: "#93c5fd" }
              : { fg: "#7c3aed", bg: "#ede9fe", ring: "#c4b5fd" }
            : colorForDisplayIdx(displayIdx);
          const t = new Date(s.captured_at).getTime();
          const inWindow =
            !isConv &&
            winStart !== null &&
            t >= winStart &&
            (winEnd === null || t <= winEnd);

          // 用户命令 → 右侧；Echo 回复 / STT 转写 → 左侧
          const isSelf = isUserCmd;

          // 同源合并：上一条是相同 speaker / 同种 conv kind 才合并头像
          const prev = idx > 0 ? segs[idx - 1] : null;
          const sameSpeakerAsPrev = isConv
            ? prev !== null && prev.convKind === s.convKind
            : prev !== null &&
              !prev.convKind &&
              prev.speaker_label === s.speaker_label;

          const containerSpacing = sameSpeakerAsPrev ? "mt-1" : "mt-4";
          const displayLabel = isUserCmd
            ? "我"
            : isEchoReply
              ? "Echo"
              : displayIdx > 0
                ? `说话人 ${displayIdx}`
                : "未识别";

          const avatarLetter = isUserCmd
            ? "我"
            : isEchoReply
              ? "E"
              : displayIdx > 0
                ? String(displayIdx)
                : "?";
          const avatar = (
            <div
              className="shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-[12px] font-semibold select-none"
              style={{
                color: c.fg,
                background: c.bg,
                boxShadow: `inset 0 0 0 1px ${c.ring}`,
              }}
              title={
                isConv
                  ? displayLabel
                  : s.speaker_label
                    ? `${displayLabel}（全局编号 ${s.speaker_label}）`
                    : "未识别说话人"
              }
              data-testid={
                isConv ? `conv-avatar-${s.convKind}` : "speaker-avatar"
              }
            >
              {avatarLetter}
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
                  className={`relative text-[14px] leading-6 px-3.5 py-2 rounded-2xl shadow-sm border break-words whitespace-pre-wrap ${
                    inWindow
                      ? "border-amber-300/70 ring-1 ring-amber-200/60"
                      : "border-paper-300"
                  } ${
                    isUserCmd
                      ? "bg-blue-600 text-white border-blue-600"
                      : isEchoReply
                        ? "bg-violet-50 text-ink-900 border-violet-200"
                        : "bg-white text-ink-800"
                  }`}
                  data-testid={
                    isConv ? `conv-bubble-${s.convKind}` : "transcript-bubble"
                  }
                >
                  {s.text}
                  {s.convStatus === "pending" && (
                    <span className="inline-flex items-center gap-1 ml-1.5 text-[11px] opacity-80">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      Echo 思考中…
                    </span>
                  )}
                  {s.convKind === "rag_answer" &&
                    s.convCitations &&
                    s.convCitations.length > 0 && (
                      <div className="mt-1.5 pt-1.5 border-t border-violet-200/60 text-[10.5px] text-ink-500 flex flex-wrap gap-x-2 gap-y-0.5">
                        引用：
                        {s.convCitations.slice(0, 5).map((cit) => (
                          <span
                            key={cit.chunk_id ?? cit.doc_id}
                            className="font-mono"
                            title={cit.chunk_id ?? cit.doc_id}
                          >
                            {cit.doc_id.slice(0, 24)}
                          </span>
                        ))}
                      </div>
                    )}
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
