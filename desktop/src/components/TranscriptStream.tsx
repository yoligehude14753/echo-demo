import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bot, Brain, Clock3, UserRound } from "lucide-react";
import {
  artifactDownloadUrl,
  artifactIdFromDownloadHref,
  listRecentAmbient,
  type AmbientSegment,
} from "@/api";
import AuthenticatedDownloadLink from "@/components/AuthenticatedDownloadLink";
import { shouldHideSharedPublicHistory } from "@/runtime";
import { useStore } from "@/store";
import type { MemorySourceCard, TranscriptSegment } from "@/types";
import {
  buildSpeakerDisplayMap,
  colorForDisplayIdx,
} from "@/lib/speakerDisplay";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";

function fmtClockShort(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/**
 * 统一对话流：逐字稿、用户问题与 AI 回复按时间顺序呈现，来源通过头像、
 * 标签和颜色区分，不再要求用户在两个页面之间来回切换。
 *
 * 数据源切换（P4 M_meeting_history，2026-05-28）：
 * - currentMeetingId === null（"实时记录"）→ 显示全局 ambient feed（3s 轮询）
 * - currentMeetingId 已选 + 会议 ended/finalized → 显示 meeting.segments（DB 历史）
 * - currentMeetingId 已选 + 会议 in_meeting → 显示 ambient + 时间窗高亮（兼容当前
 *   ambient pipeline：会议中 chunk 既写 ambient_segments 也走 meeting overlay；
 *   两边内容一致但 ambient 更新更快，所以现场仍用 ambient）
 *
 * 新增的 echodesk-stream* 语义 class 让逐字稿可采用扁平记录样式，同时保留
 * 现有 testid 与基础 utility class，便于分阶段迁移视觉层。
 */

interface DisplaySegment {
  text: string;
  captured_at: string;
  speaker_label: string | null;
  role?: "speaker" | "user" | "assistant" | "memory";
  memorySources?: MemorySourceCard[];
  memoryLabel?: string;
  memoryModel?: string;
}

function MemoryAssociationCard({ segment }: { segment: DisplaySegment }): JSX.Element {
  const sources = segment.memorySources ?? [];
  const renderSource = (source: MemorySourceCard): JSX.Element => (
    <div
      key={source.candidate_id}
      className="flex gap-2.5 py-2.5 border-t border-indigo-100 first:border-t-0"
      data-testid="memory-source-item"
      data-source-ref={source.source_ref}
    >
      <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-50 text-[11px] font-semibold text-indigo-700">
        {source.index}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[13px] font-medium text-ink-900">{source.title}</span>
          <span className="shrink-0 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">
            {source.level}
          </span>
        </div>
        <p className="mt-0.5 line-clamp-2 text-[12px] leading-5 text-ink-600">
          {source.excerpt}
        </p>
        <div className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-400">
          <Clock3 className="h-3 w-3" aria-hidden="true" />
          <span>{fmtClockShort(source.occurred_at)}</span>
          <span>·</span>
          <span className="truncate">{source.relation}</span>
        </div>
      </div>
    </div>
  );

  return (
    <div className="mt-3 ml-10 max-w-[78%]" data-testid="memory-association-card">
      <div className="rounded-2xl border border-indigo-200 bg-white px-4 py-3 shadow-sm">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-[13px] font-medium text-indigo-800">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-50 ring-1 ring-inset ring-indigo-200">
              <Brain className="h-3.5 w-3.5" aria-hidden="true" />
            </span>
            <span>Echo Memory</span>
          </div>
          <span className="text-[10px] text-ink-400">{segment.memoryModel ?? "qwen3 8b"}</span>
        </div>
        <div className="mt-2 text-[14px] text-ink-800">
          {segment.memoryLabel ?? `找到 ${sources.length} 条相关历史信息`}
        </div>
        <div className="mt-1">{sources.slice(0, 3).map(renderSource)}</div>
        {sources.length > 3 && (
          <details className="border-t border-indigo-100 pt-2">
            <summary className="cursor-pointer select-none text-[11px] text-indigo-700">
              展开其余 {sources.length - 3} 条来源
            </summary>
            <div>{sources.slice(3).map(renderSource)}</div>
          </details>
        )}
      </div>
    </div>
  );
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

function normalizeSpokenText(text: string): string {
  return text
    .replace(/[\s，。！？、,.!?;；:"“”'‘’（）()[\]【】<>《》]/g, "")
    .toLowerCase();
}

function editDistance(a: string, b: string): number {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  const curr = new Array<number>(b.length + 1);
  for (let i = 1; i <= a.length; i += 1) {
    curr[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost);
    }
    for (let j = 0; j <= b.length; j += 1) prev[j] = curr[j];
  }
  return prev[b.length];
}

function likelySameSpokenText(a: string, b: string): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  const shorter = a.length <= b.length ? a : b;
  const longer = a.length > b.length ? a : b;
  if (shorter.length >= 8 && longer.includes(shorter)) return true;
  if (longer.length > 80) return false;
  const distance = editDistance(a, b);
  const similarity = 1 - distance / Math.max(a.length, b.length);
  return similarity >= 0.82;
}

function dedupeDisplaySegments(segments: DisplaySegment[]): DisplaySegment[] {
  const out: DisplaySegment[] = [];
  for (const seg of segments) {
    const textKey = normalizeSpokenText(seg.text);
    if (!textKey) continue;
    const ts = new Date(seg.captured_at).getTime();
    const prev = out[out.length - 1];
    if (prev) {
      const prevKey = normalizeSpokenText(prev.text);
      const prevTs = new Date(prev.captured_at).getTime();
      const sameSpeaker =
        (prev.speaker_label ?? prev.role ?? "") ===
        (seg.speaker_label ?? seg.role ?? "");
      const near = Number.isFinite(ts) && Number.isFinite(prevTs)
        ? Math.abs(ts - prevTs) <= 12_000
        : false;
      if (near && (prevKey === textKey || (sameSpeaker && likelySameSpokenText(prevKey, textKey)))) {
        continue;
      }
    }
    out.push(seg);
  }
  return out;
}

export default function TranscriptStream(): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const [ambient, setAmbient] = useState<AmbientSegment[]>([]);
  const events = useStore((s) => s.events);
  const localAmbient = useStore((s) => s.ambientSegments);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickyToBottomRef = useRef(true);
  const localOnlyAmbient = shouldHideSharedPublicHistory();

  useEffect(() => {
    setAmbient([]);
  }, [backendOriginRevision]);

  // 是否走"会议历史"分支：会议已选 + 已结束（ended/finalized 等）+ 有 segments
  // 进行中会议仍走 ambient 分支保持实时性（ambient 是 chunk 写入的最近 100 条）
  // 但 public/TV 模式会屏蔽共享 WS 和 /capture/recent，会议实时段只能来自本机
  // /capture/chunk 回包，所以进行中会议也要优先显示 meeting.segments。
  const showMeetingHistory =
    currentMeetingId !== null &&
    meeting !== undefined &&
    (meeting.state === "ended" || localOnlyAmbient) &&
    meeting.segments.length > 0;

  const baseSegs: DisplaySegment[] = useMemo(() => {
    if (showMeetingHistory && meeting) {
      return meeting.segments.map((s) =>
        meetingSegmentToDisplay(s, meeting.started_at),
      );
    }
    const source = localOnlyAmbient ? localAmbient : ambient;
    const sourceDisplay = source.map(ambientToDisplay);
    if (currentMeetingId !== null && meeting?.started_at) {
      const start = new Date(meeting.started_at).getTime();
      const end = meeting.ended_at ? new Date(meeting.ended_at).getTime() : null;
      return sourceDisplay.filter((seg) => {
        const t = new Date(seg.captured_at).getTime();
        return Number.isFinite(t) && t >= start && (end === null || t <= end);
      });
    }
    return sourceDisplay;
  }, [
    showMeetingHistory,
    meeting,
    currentMeetingId,
    localOnlyAmbient,
    localAmbient,
    ambient,
  ]);

  const dialogSegs: DisplaySegment[] = useMemo(() => {
    const out: DisplaySegment[] = [];
    for (const ev of events.slice(-80)) {
      const eventMeetingId = ev.meeting_id ?? null;
      if (
        currentMeetingId !== null
          ? eventMeetingId !== currentMeetingId
          : eventMeetingId !== null
      ) {
        continue;
      }
      const payload = (ev.payload ?? {}) as {
        question?: unknown;
        answer?: unknown;
        label?: unknown;
        model_display_name?: unknown;
        sources?: unknown;
      };
      if (ev.type === "rag.query") {
        const question =
          typeof payload.question === "string" ? payload.question.trim() : "";
        if (question) {
          out.push({
            text: question,
            captured_at: ev.ts,
            speaker_label: "user",
            role: "user",
          });
        }
      }
      if (ev.type === "rag.answer.done" || ev.type === "chat.done") {
        const answer =
          typeof payload.answer === "string" ? payload.answer.trim() : "";
        if (answer) {
          out.push({
            text: answer,
            captured_at: ev.ts,
            speaker_label: "assistant",
            role: "assistant",
          });
        }
      }
      if (ev.type === "memory.sources" && Array.isArray(payload.sources)) {
        const sources = payload.sources as MemorySourceCard[];
        if (sources.length > 0) {
          out.push({
            text: typeof payload.label === "string" ? payload.label : "相关历史信息",
            captured_at: ev.ts,
            speaker_label: "memory",
            role: "memory",
            memorySources: sources,
            memoryLabel: typeof payload.label === "string" ? payload.label : undefined,
            memoryModel: typeof payload.model_display_name === "string"
              ? payload.model_display_name
              : undefined,
          });
        }
      }
    }
    return out.slice(-20);
  }, [currentMeetingId, events]);

  const segs: DisplaySegment[] = useMemo(() => {
    return dedupeDisplaySegments(
      [...baseSegs, ...dialogSegs].sort((a, b) =>
        a.captured_at.localeCompare(b.captured_at),
      ),
    );
  }, [baseSegs, dialogSegs]);

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
    if (localOnlyAmbient) {
      return undefined;
    }
    if (showMeetingHistory) {
      return undefined;
    }
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const tick = async (): Promise<void> => {
      try {
        const recent = await listRecentAmbient(100, {
          signal: controller.signal,
        });
        if (
          alive &&
          isCurrent(originGeneration) &&
          !controller.signal.aborted
        ) {
          setAmbient(recent);
        }
      } catch {
        /* 静默 */
      }
    };
    void tick();
    const t = setInterval(tick, 3_000);
    return () => {
      alive = false;
      unregisterController();
      clearInterval(t);
    };
  }, [
    backendOriginRevision,
    captureGeneration,
    isCurrent,
    localOnlyAmbient,
    registerAbortController,
    showMeetingHistory,
  ]);

  useEffect(() => {
    if (localOnlyAmbient) return;
    if (showMeetingHistory) return;
    if (!events.length) return;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const last = events[events.length - 1];
    if (
      last.type === "meeting.segment" ||
      last.type === "meeting.auto_detected" ||
      last.type === "meeting.state_changed"
    ) {
      void listRecentAmbient(100, { signal: controller.signal })
        .then((r) => {
          if (
            isCurrent(originGeneration) &&
            !controller.signal.aborted
          ) {
            setAmbient(r);
          }
        })
        .catch(() => undefined);
    }
    return unregisterController;
  }, [
    backendOriginRevision,
    captureGeneration,
    events,
    isCurrent,
    localOnlyAmbient,
    registerAbortController,
    showMeetingHistory,
  ]);

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
        <div className="echodesk-transcript-empty flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
          <div>该会议未保存逐字稿</div>
          <div className="text-[10px] text-ink-300">
            会议期间可能没有检测到可识别的语音
          </div>
        </div>
      );
    }
    return (
      <div className="echodesk-transcript-empty flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
        <div>从这里开始对话</div>
        <div className="text-[10px] text-ink-300">
          {localOnlyAmbient
            ? "语音转录和 AI 回复会按时间显示在同一条对话里"
            : "直接开口或在下方输入问题，转录和 AI 回复会出现在同一条对话里"}
        </div>
      </div>
    );
  }

  return (
    <div
      ref={scrollerRef}
      className="echodesk-stream echodesk-stream--conversation flex-1 min-h-0 overflow-y-auto px-6 py-4"
      data-testid="transcript-scroller"
      data-mode={
        showMeetingHistory
          ? meeting?.state === "ended"
            ? "meeting-history"
            : "meeting-live-local"
          : "ambient"
      }
    >
      <div className="echodesk-stream-list max-w-3xl mx-auto">
        {segs.map((s, idx) => {
          if (s.role === "memory") {
            return <MemoryAssociationCard key={`${s.captured_at}-${idx}`} segment={s} />;
          }
          const displayIdx = s.speaker_label
            ? (speakerDisplayMap.get(s.speaker_label) ?? 0)
            : 0;
          const speakerColor = colorForDisplayIdx(displayIdx);
          const t = new Date(s.captured_at).getTime();
          const inWindow =
            winStart !== null &&
            t >= winStart &&
            (winEnd === null || t <= winEnd);

          const isAssistant = s.role === "assistant";
          const isSelf = s.role === "user";
          const c = isSelf
            ? { fg: "#1d4ed8", bg: "#eff6ff", ring: "#bfdbfe" }
            : speakerColor;

          // 连续同说话人合并：只在第一条显示头像，气泡间距收紧
          const prev = idx > 0 ? segs[idx - 1] : null;
          const sameSpeakerAsPrev =
            prev !== null &&
            prev.speaker_label === s.speaker_label &&
            prev.role === s.role;

          const containerSpacing = sameSpeakerAsPrev ? "mt-1" : "mt-4";
          const displayLabel =
            isAssistant
              ? "Echo AI"
              : isSelf
                ? "你"
                : displayIdx > 0
                  ? `说话人 ${displayIdx}`
                  : "未识别";

          // 头像：32px 圆形，背景柔色 + 同色边框，居中数字
          const avatar = (
            <div
              className={`echodesk-stream-avatar shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-[12px] font-semibold select-none ${
                isAssistant
                  ? "bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-200"
                  : ""
              }`}
              style={
                isAssistant
                  ? undefined
                  : {
                      color: c.fg,
                      background: c.bg,
                      boxShadow: `inset 0 0 0 1px ${c.ring}`,
                    }
              }
              title={
                isAssistant
                  ? displayLabel
                  : s.speaker_label
                    ? displayLabel
                    : "未识别说话人"
              }
              data-testid="speaker-avatar"
            >
              {isAssistant ? (
                <span
                  className="inline-flex"
                  data-testid="echo-ai-avatar"
                  aria-hidden="true"
                >
                  <Bot className="h-3.5 w-3.5" />
                </span>
              ) : isSelf ? (
                <UserRound className="h-3.5 w-3.5" aria-hidden="true" />
              ) : displayIdx > 0 ? (
                displayIdx
              ) : (
                <UserRound className="h-3.5 w-3.5" aria-hidden="true" />
              )}
            </div>
          );
          const avatarSpacer = (
            <div className="shrink-0 w-8 h-8" aria-hidden="true" />
          );

          return (
            <div
              key={`${s.captured_at}-${idx}`}
              className={`echodesk-stream-row echodesk-stream-row--${
                isAssistant ? "assistant" : isSelf ? "user" : "speaker"
              } group flex gap-2 items-end ${containerSpacing} ${
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
                    className={`echodesk-stream-speaker text-[11px] mb-0.5 px-1 ${
                      isSelf ? "text-right" : "text-left"
                    } ${isAssistant ? "text-indigo-700" : ""}`}
                    style={isAssistant ? undefined : { color: c.fg }}
                    data-testid="speaker-tag"
                  >
                    {isAssistant ? (
                      <span data-testid="echo-ai-label">{displayLabel}</span>
                    ) : (
                      displayLabel
                    )}
                  </div>
                )}
                <div
                  className={`echodesk-stream-message echodesk-stream-message--${
                    isAssistant ? "assistant" : isSelf ? "user" : "speaker"
                  } relative text-[14px] leading-6 px-3.5 py-2 rounded-2xl shadow-sm border break-words ${
                    inWindow
                      ? "border-amber-300/70 ring-1 ring-amber-200/60"
                      : "border-paper-300"
                  } ${
                    isSelf
                      ? "bg-blue-500 text-white border-blue-500"
                      : isAssistant
                        ? "bg-indigo-50 text-ink-900 border-indigo-200"
                        : "bg-white text-ink-800"
                  }`}
                  data-testid={
                    isAssistant
                      ? "assistant-message"
                      : isSelf
                        ? "user-message"
                        : "transcript-message"
                  }
                >
                  {isAssistant ? (
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        h1: ({ children }) => (
                          <h1 className="text-[18px] font-semibold leading-7 mt-1 mb-2">
                            {children}
                          </h1>
                        ),
                        h2: ({ children }) => (
                          <h2 className="text-[16px] font-semibold leading-7 mt-1 mb-2">
                            {children}
                          </h2>
                        ),
                        h3: ({ children }) => (
                          <h3 className="text-[15px] font-semibold leading-6 mt-2 mb-1">
                            {children}
                          </h3>
                        ),
                        p: ({ children }) => <p className="my-1">{children}</p>,
                        ul: ({ children }) => (
                          <ul className="list-disc pl-5 my-1 space-y-0.5">{children}</ul>
                        ),
                        ol: ({ children }) => (
                          <ol className="list-decimal pl-5 my-1 space-y-0.5">{children}</ol>
                        ),
                        li: ({ children }) => <li className="pl-0.5">{children}</li>,
                        strong: ({ children }) => (
                          <strong className="font-semibold text-ink-950">{children}</strong>
                        ),
                        a: ({ children, href }) => {
                          const artifactId = artifactIdFromDownloadHref(href);
                          return artifactId ? (
                            <AuthenticatedDownloadLink
                              url={artifactDownloadUrl(artifactId)}
                              className="text-blue-600 underline underline-offset-2"
                            >
                              {children}
                            </AuthenticatedDownloadLink>
                          ) : (
                            <a
                              href={href}
                              target="_blank"
                              rel="noreferrer"
                              className="text-blue-600 underline underline-offset-2"
                            >
                              {children}
                            </a>
                          );
                        },
                        code: ({ children }) => (
                          <code className="rounded bg-white/80 px-1 py-0.5 text-[13px] font-mono text-ink-800">
                            {children}
                          </code>
                        ),
                        blockquote: ({ children }) => (
                          <blockquote className="border-l-2 border-indigo-300 pl-3 my-2 text-ink-700">
                            {children}
                          </blockquote>
                        ),
                      }}
                    >
                      {s.text}
                    </ReactMarkdown>
                  ) : (
                    s.text
                  )}
                </div>
                <span
                  className={`echodesk-stream-time mt-1 px-1 text-[10px] text-ink-400 tabular-nums whitespace-nowrap select-none ${
                    isSelf ? "self-end text-right" : "self-start text-left"
                  }`}
                  data-testid="transcript-time"
                >
                  {fmtClockShort(s.captured_at)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
