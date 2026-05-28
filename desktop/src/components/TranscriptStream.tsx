import { useEffect, useMemo, useRef, useState } from "react";
import { listRecentAmbient, type AmbientSegment } from "@/api";
import { useStore } from "@/store";
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
 * 视觉规则（来自用户 2026-05-27 反馈）：
 * - 所有"非用户手动输入"的文本（ambient 转写）→ 左侧气泡 + 头像在左
 * - 用户手动输入（未来：CommandBar 标注 self 的回话）→ 右侧气泡 + 头像在右
 *   当前 ambient 没有 is_self 字段 → 全部走左侧 lane，预留右侧给未来
 * - 头像 = 32×32 圆形彩色 + 居中数字（数字就是 remap 后的"说话人 N"序号）
 * - 时间默认隐藏，hover 整条 → 显示 HH:MM（精度到分够用）
 * - 同一说话人连续多条：合并头像（只在第一条显示），间距更紧（4px）；
 *   切换说话人时拉开间距（16px），更易扫读
 *
 * 数据：3s 轮询 /capture/recent + WS 事件触发立即刷新（与 v1 相同）
 */
export default function TranscriptStream(): JSX.Element {
  const [segs, setSegs] = useState<AmbientSegment[]>([]);
  const events = useStore((s) => s.events);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickyToBottomRef = useRef(true);

  const speakerDisplayMap = useMemo(
    () => buildSpeakerDisplayMap(segs),
    [segs],
  );

  const winStart = meeting?.started_at
    ? new Date(meeting.started_at).getTime()
    : null;
  const winEnd = meeting?.ended_at ? new Date(meeting.ended_at).getTime() : null;

  useEffect(() => {
    let alive = true;
    const tick = async (): Promise<void> => {
      try {
        const recent = await listRecentAmbient(100);
        if (alive) setSegs(recent);
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
  }, []);

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

  // segs 从空 → 非空时重新绑监听（首次出现 scroller 需要 attach onScroll）。
  // 之前直接写 `[segs.length === 0]` 表达式触发 react-hooks/exhaustive-deps 警告
  // （PR #51 引入），改用 useMemo 抽出来让静态检查能通过。
  const hasNoSegs = useMemo(() => segs.length === 0, [segs.length]);
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = (): void => {
      const distFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
      stickyToBottomRef.current = distFromBottom < 40;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [hasNoSegs]);

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
      className="flex-1 min-h-0 overflow-y-auto px-6 py-4"
      data-testid="transcript-scroller"
    >
      <div className="max-w-3xl mx-auto">
        <div className="text-[11px] text-ink-400 mb-3 px-1 flex items-center gap-2 sticky top-0 bg-paper-50/90 backdrop-blur-sm py-1 z-10">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span>ambient 持续转写 · {segs.length} 条 · 每 3s 刷新</span>
          {meeting && (
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
