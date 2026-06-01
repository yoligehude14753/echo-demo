import { useEffect, useMemo, useRef, useState } from "react";
import { FileCode, FileSpreadsheet, FileText, FileType2, Globe, Loader2, Pencil, Presentation } from "lucide-react";
import { Tooltip, message } from "antd";
import { listRecentAmbient, renameSpeaker, type AmbientSegment } from "@/api";
import { useStore, type ConversationEvent } from "@/store";
import type { GeneratedArtifact, TranscriptSegment } from "@/types";
import { CitationList, CitationText } from "@/components/CitationText";
import ArtifactPreviewModal from "@/components/ArtifactPreviewModal";
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
 * - currentMeetingId === null（"伴随时段"）→ 显示全局 ambient feed（3s 轮询）
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
  // 用户 2026-05-28：speaker_id 沿着 STT → ambient/meeting → display 一路带下来
  // 让 speaker_tag 点击改名时知道改的是哪个全局 ID（rename API 需要）。
  speaker_id?: string | null;
  /**
   * 用户 2026-05-28 反馈：CommandBar 输入要进转写流（右），Echo / RAG 回复
   * 要在转写流（左）。同源合并：所有 conversation events 转成 DisplaySegment，
   * convKind 决定渲染样式：
   *   - user_command → 右侧 + 紫色 "用户" 头像（2026-05-28：原来叫"我"，改"用户"）
   *   - assistant_reply → 左侧 + Echo "E" 头像 + 高亮气泡
   *   - rag_answer → 左侧 + Echo "E" 头像 + 引用列表
   * undefined → STT 真实 segment（保持原路径）
   */
  convKind?: ConversationEvent["kind"];
  convStatus?: ConversationEvent["status"];
  convCitations?: ConversationEvent["citations"];
  convArtifacts?: ConversationEvent["artifacts"];
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
    convArtifacts: ev.artifacts,
    convId: ev.id,
  };
}

const artifactIcon: Record<string, JSX.Element> = {
  html: <Globe className="w-3.5 h-3.5" />,
  pptx: <Presentation className="w-3.5 h-3.5" />,
  ppt: <Presentation className="w-3.5 h-3.5" />,
  xlsx: <FileSpreadsheet className="w-3.5 h-3.5" />,
  excel: <FileSpreadsheet className="w-3.5 h-3.5" />,
  word: <FileText className="w-3.5 h-3.5" />,
  docx: <FileText className="w-3.5 h-3.5" />,
  markdown: <FileCode className="w-3.5 h-3.5" />,
  pdf: <FileType2 className="w-3.5 h-3.5" />,
  txt: <FileText className="w-3.5 h-3.5" />,
};

function ConversationArtifactList({
  artifacts = [],
  onOpen,
}: {
  artifacts?: GeneratedArtifact[];
  onOpen: (artifact: GeneratedArtifact) => void;
}): JSX.Element | null {
  if (artifacts.length === 0) return null;
  return (
    <div className="mt-2 pt-2 border-t border-violet-200/70 space-y-1.5">
      {artifacts.map((artifact) => (
        <button
          key={artifact.artifact_id}
          type="button"
          data-testid="conversation-artifact-card"
          data-artifact-id={artifact.artifact_id}
          onClick={() => onOpen(artifact)}
          className="w-full flex items-center gap-2 rounded-lg border border-violet-200 bg-white/85 px-2.5 py-2 text-left hover:bg-violet-50 transition"
        >
          <span className="inline-flex items-center gap-1 rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-violet-700">
            {artifactIcon[artifact.artifact_type] ?? null}
            {artifact.artifact_type}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[12px] font-medium text-ink-800">
              {artifact.title || artifact.artifact_id}
            </span>
            <span className="block truncate font-mono text-[10px] text-ink-400">
              {artifact.artifact_id}
            </span>
          </span>
          <span className="text-[10px] text-violet-600">打开</span>
        </button>
      ))}
    </div>
  );
}

function ambientToDisplay(s: AmbientSegment): DisplaySegment {
  return {
    text: s.text,
    captured_at: s.captured_at,
    speaker_label: s.speaker_label,
    speaker_id: s.speaker_id,
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
    speaker_id: s.speaker_id ?? null,
  };
}

export default function TranscriptStream(): JSX.Element {
  const [ambient, setAmbient] = useState<AmbientSegment[]>([]);
  const events = useStore((s) => s.events);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const meetingDetailLoaded = useStore((s) =>
    currentMeetingId ? Boolean(s.meetingDetailLoaded[currentMeetingId]) : true,
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickyToBottomRef = useRef(true);
  const [activeCitationKey, setActiveCitationKey] = useState<string | null>(null);
  const [previewArtifact, setPreviewArtifact] = useState<GeneratedArtifact | null>(null);

  // 用户 2026-05-28：所有 speaker label 可改名。
  // 本地 overrides 优先：刚改完不等 ws 事件就能显示新名字；同时立刻 POST /speakers/{id}/rename
  // 持久化到 repo（label_user_set=1），下次同声纹再来或重启后能识别。
  // key = speaker_id（全局唯一），value = 用户起的名字
  const [speakerOverrides, setSpeakerOverrides] = useState<
    Record<string, string>
  >({});

  // 是否走"会议历史"分支：会议已选 + 已结束（ended/finalized 等）
  // 进行中会议仍走 ambient 分支保持实时性（ambient 是 chunk 写入的最近 100 条）
  const showMeetingHistory =
    currentMeetingId !== null &&
    meeting !== undefined &&
    meeting.state === "ended";

  const conversationEvents = useStore((s) => s.conversationEvents);
  const scopedConversationEvents = useMemo(
    () =>
      conversationEvents.filter(
        (ev) => (ev.meeting_id ?? null) === currentMeetingId,
      ),
    [conversationEvents, currentMeetingId],
  );

  // 合并 STT segments + 人机对话事件，按 ts 升序排
  const segs: DisplaySegment[] = useMemo(() => {
    const base: DisplaySegment[] =
      showMeetingHistory && meeting
        ? meeting.segments.map((s) =>
            meetingSegmentToDisplay(s, meeting.started_at),
          )
        : ambient.map(ambientToDisplay);
    if (scopedConversationEvents.length === 0) return base;
    const convs = scopedConversationEvents.map(convToDisplay);
    const merged = [...base, ...convs];
    merged.sort((a, b) =>
      new Date(a.captured_at).getTime() - new Date(b.captured_at).getTime(),
    );
    return merged;
  }, [showMeetingHistory, meeting, ambient, scopedConversationEvents]);

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
      if (!meetingDetailLoaded) {
        return (
          <div className="flex-1 min-h-0 flex items-center justify-center text-ink-400 text-[12px] flex-col gap-2">
            <div>正在加载该会议转写…</div>
            <div className="text-[10px] text-ink-300">
              切换会议后只显示当前会议内容，不再回退到伴随时段
            </div>
          </div>
        );
      }
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

          // 用户起的名字（本地 override 或来自 backend label）优先于「说话人 N」
          // 即使没有 speaker_id（旧 ambient 记录）也按 speaker_label 兜底匹配
          const sttUserName =
            (s.speaker_id && speakerOverrides[s.speaker_id]) || null;

          const displayLabel = isUserCmd
            // 用户 2026-05-28："我" → "用户"，"用户"语义更对称
            ? "用户"
            : isEchoReply
              ? "Echo"
              : sttUserName
                ? sttUserName
                : displayIdx > 0
                  ? `说话人 ${displayIdx}`
                  : "未识别";

          const avatarLetter = isUserCmd
            // 用户头像同样 "我" → "用"
            ? "用"
            : isEchoReply
              ? "E"
              : sttUserName
                ? sttUserName.slice(0, 1)
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

          // STT 说话人可改名（user_command / echo_reply 不需改）
          const canRename = !isConv && Boolean(s.speaker_id);
          const handleRenameClick = (): void => {
            if (!s.speaker_id) return;
            const next = window.prompt(
              `给 ${displayLabel} 起个名字（如：陈志鹏）：`,
              sttUserName ?? "",
            );
            if (next === null) return;
            const trimmed = next.trim();
            if (!trimmed) return;
            // 本地 override 立刻生效，避免等 ws
            setSpeakerOverrides((prev) => ({
              ...prev,
              [s.speaker_id as string]: trimmed,
            }));
            void renameSpeaker(s.speaker_id, trimmed)
              .then(() => {
                message.success(`已重命名为「${trimmed}」`);
              })
              .catch((e: unknown) => {
                const msg = e instanceof Error ? e.message : String(e);
                message.error(`改名失败：${msg}`);
                // 回滚 override
                setSpeakerOverrides((prev) => {
                  const rest = { ...prev };
                  delete rest[s.speaker_id as string];
                  return rest;
                });
              });
          };

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
                    className={`text-[11px] mb-0.5 px-1 inline-flex items-center gap-1 ${
                      isSelf ? "text-right justify-end" : "text-left"
                    }`}
                    style={{ color: c.fg }}
                    data-testid="speaker-tag"
                  >
                    <span>{displayLabel}</span>
                    {canRename && (
                      <Tooltip
                        title={
                          sttUserName
                            ? "改名（声纹将永久关联此名字）"
                            : "起个名字（声纹将永久关联此名字）"
                        }
                      >
                        <button
                          type="button"
                          onClick={handleRenameClick}
                          className="opacity-0 group-hover:opacity-70 hover:!opacity-100 transition cursor-pointer"
                          aria-label="重命名说话人"
                          data-testid="speaker-rename-btn"
                        >
                          <Pencil className="w-3 h-3" />
                        </button>
                      </Tooltip>
                    )}
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
                        ? `bg-violet-50 text-ink-900 border-violet-200${
                            s.convStatus === "pending"
                              ? " animate-pulse"
                              : ""
                          }`
                        : "bg-white text-ink-800"
                  }`}
                  data-testid={
                    isConv ? `conv-bubble-${s.convKind}` : "transcript-bubble"
                  }
                >
                  {/* P4-fix（2026-05-28）：loading spinner 只在 Echo 回复气泡且 pending 时显示。
                      流式 skill / artifact 阶段（2026-05-28 升级）：pending 时 text 持续
                      被 SSE 进度事件 patch（如「准备 prompt 中…」「已收到 N 字符…」），
                      所以 spinner 改成 text 前方的小图标位（不再 trailing"思考中…"），
                      让用户感知"过程性内容在持续刷新"。text 为空时回退老版"思考中"提示。 */}
                  {isEchoReply && s.convStatus === "pending" && s.text ? (
                    <Loader2
                      className="inline w-3 h-3 animate-spin -mt-0.5 mr-1.5 text-violet-600"
                      aria-label="生成中"
                    />
                  ) : null}
                  {isEchoReply && s.convCitations && s.convCitations.length > 0 ? (
                    <CitationText
                      text={s.text}
                      citations={s.convCitations}
                      appendUnreferenced={s.convKind === "rag_answer"}
                      activeKey={activeCitationKey}
                      onActiveKeyChange={setActiveCitationKey}
                    />
                  ) : (
                    s.text
                  )}
                  {isEchoReply && s.convStatus === "pending" && !s.text && (
                    <span className="inline-flex items-center gap-1 text-[11px] opacity-80">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      Echo 思考中…
                    </span>
                  )}
                  {s.convKind === "rag_answer" && s.convCitations && (
                    <CitationList
                      citations={s.convCitations}
                      activeKey={activeCitationKey}
                      onActiveKeyChange={setActiveCitationKey}
                    />
                  )}
                  {isEchoReply && (
                    <ConversationArtifactList
                      artifacts={s.convArtifacts}
                      onOpen={setPreviewArtifact}
                    />
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
      <ArtifactPreviewModal
        artifact={previewArtifact}
        onClose={() => setPreviewArtifact(null)}
      />
    </div>
  );
}
