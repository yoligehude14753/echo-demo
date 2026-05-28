import { create } from "zustand";
import type {
  EchoEvent,
  GeneratedArtifact,
  MeetingCard,
  MeetingMinutes,
  TodoItem,
  TranscriptSegment,
} from "@/types";
import type { MeetingSummary } from "@/api";
import {
  buildFailedArtifact,
  FAILED_ARTIFACT_LIMIT,
  type FailedArtifact,
} from "@/lib/failedArtifact";

/**
 * M_minutes_refactor：MinutesView 的「执行待办」按钮通过 store.prefillCommandBar
 * 把 todo.suggested_command 推送给 CommandBar。CommandBar 启动时注册一个
 * handler；store 持有该 handler 引用，并暴露 prefillCommandBar(text, meta) 给
 * 任何组件调用。
 *
 * 这条间接路径替代了「父组件 props 透传 ref」的方案——MinutesView 与
 * CommandBar 在 App 树里非直接父子，走 store 单例最简单且与 sub_J 的 chat
 * 分支彻底解耦。
 */
export interface CommandBarPrefillMeta {
  meeting_id?: string;
  todo_id?: string;
}
export type CommandBarPrefillHandler = (
  text: string,
  meta?: CommandBarPrefillMeta,
) => void;

/**
 * 人机会话流条目（CommandBar 用户输入 + Echo / RAG / chat 回复）。
 *
 * 用户 2026-05-28 反馈：「我输入的命令也要进转写流（右边），Echo 的回答
 * 也要进转写流（左边），RAG 答案同理」——TranscriptStream 此前只展示
 * STT 转写，CommandBar 提交后只 toast 一条 success，正文丢失。
 *
 * 设计选择：不在 backend 落库，纯前端 in-memory；进程内可见，重启清空。
 * 后续若要持久化，挪到 sqlite 的 ambient_segments 加 `kind` 字段。
 */
export interface ConversationEvent {
  id: string;
  /** ISO 时间戳，跟 STT segments 合并排序用 */
  ts: string;
  kind: "user_command" | "assistant_reply" | "rag_answer";
  text: string;
  /** RAG 答案的引用，UI 显示 `[doc:xxx]` 形式 */
  citations?: Array<{ doc_id: string; chunk_id?: string; score?: number }>;
  /** 命令派发后的状态：'pending' 时 UI 显示「Echo 思考中…」spinner */
  status?: "pending" | "done" | "failed";
}

interface Store {
  meetings: Record<string, MeetingCard>;
  currentMeetingId: string | null;
  /** 人机会话事件流（CommandBar 输入 + LLM 回答），跟 STT segments 同源合并渲染。 */
  conversationEvents: ConversationEvent[];
  /**
   * 标记 meeting 详情已经从后端 detail endpoint（transcript/minutes/artifacts）
   * 拉过一次。避免每次切换都重复 fetch；新事件到达时（meeting.segment 等）store
   * 自然会通过 applyEvent 增量更新，无需重置该 flag。
   */
  meetingDetailLoaded: Record<string, boolean>;
  artifacts: GeneratedArtifact[];
  failedArtifacts: FailedArtifact[];
  /**
   * 暂存最近一次 artifact.generating 的 brief，按 artifact_type 索引（最新覆盖旧的）。
   * artifact.failed 到达时按 artifact_type 配对回填 intent_text；artifact.ready 时清除。
   * 仅用于 best-effort 关联，无 1:1 严格匹配（同类型并发生成会丢失旧 brief，但 P2.2 演示场景够用）。
   */
  pendingArtifactBriefs: Record<string, string>;
  connected: boolean;
  events: EchoEvent[];
  /**
   * M_minutes_refactor：CommandBar 在 mount 时注册一个 prefill handler；
   * MinutesView 「执行」按钮调 prefillCommandBar(text, meta) 触发。
   */
  _commandBarPrefillHandler: CommandBarPrefillHandler | null;

  setConnected(v: boolean): void;
  selectMeeting(id: string | null): void;
  applyEvent(e: EchoEvent): void;
  upsertMeeting(id: string, patch: Partial<MeetingCard>): void;
  /** 用 GET /meetings 返回的列表把 store.meetings 与每条 summary 合并（保留事件已注入的 segments/minutes/artifacts）。 */
  hydrateMeetings(summaries: MeetingSummary[]): void;
  /** 标记某 meeting detail 已加载完毕，避免重复 fetch。 */
  markMeetingDetailLoaded(id: string): void;
  addArtifact(a: GeneratedArtifact): void;
  /**
   * 清空全局 outputs 列表（顶栏「清空」按钮）。
   * 不清 failedArtifacts —— 它们有独立 dismiss，避免一键覆盖失败上下文。
   * 也不清 meetings[*].artifacts —— 那是会议详情视图的快照，独立维护。
   */
  clearArtifacts(): void;
  /** 删除单条产物（hover × 按钮）。也同步从所有 meeting 的 artifacts 中清掉，避免悬挂引用。 */
  removeArtifact(artifactId: string): void;
  dismissFailedArtifact(id: string): void;
  /**
   * M_minutes_refactor：CommandBar 启动时注册 prefill handler；返回的 unregister
   * 可在 unmount 时调，避免 handler 引用陈旧实例（HMR 场景）。
   */
  registerCommandBarPrefill(handler: CommandBarPrefillHandler): () => void;
  /** 把 text 推给 CommandBar 预填（meta 透传，CommandBar 据此发 artifact 时附带 meeting_id/todo_id）。 */
  prefillCommandBar(text: string, meta?: CommandBarPrefillMeta): void;

  /** CommandBar 用户提交时插入「右侧」气泡 + 返回 event id 供后续 patch（done / failed）。 */
  appendUserCommand(text: string): string;
  /** chat / rag 返回 LLM 回答时插入「左侧」Echo 气泡。
   *
   * 用户 2026-05-28：「Echo 思考中」应该在 Echo 气泡里，不是用户气泡里。
   * 所以 CommandBar 在 submit 时可立刻 append 一条 status="pending" 的空回复，
   * 让 TranscriptStream 在 Echo 气泡里渲染思考中 spinner；答复回来后调
   * ``patchAssistantReply`` 把 text / citations / status 一次性 patch 进去。
   */
  appendAssistantReply(
    text: string,
    kind?: "assistant_reply" | "rag_answer",
    citations?: ConversationEvent["citations"],
    status?: ConversationEvent["status"],
  ): string;
  /** 标记某条 user_command 已完成 / 失败（仅切 status，不动 text）。 */
  patchConversationStatus(id: string, status: "done" | "failed"): void;
  /** 答复回来后给 Echo 气泡填充 text / citations / status；其它字段不动。 */
  patchAssistantReply(
    id: string,
    patch: {
      text?: string;
      citations?: ConversationEvent["citations"];
      kind?: "assistant_reply" | "rag_answer";
      status?: "pending" | "done" | "failed";
    },
  ): void;
  /** 清空会话事件（顶栏「清空」按钮用，避免会议结束时残留）。 */
  clearConversationEvents(): void;

  reset(): void;
}

function emptyMeeting(id: string, title?: string): MeetingCard {
  return {
    meeting_id: id,
    title: title ?? id,
    state: "idle",
    segments: [],
    speakers: new Set<string>(),
    artifacts: [],
  };
}

export const useStore = create<Store>((set, get) => ({
  meetings: {},
  currentMeetingId: null,
  meetingDetailLoaded: {},
  artifacts: [],
  failedArtifacts: [],
  pendingArtifactBriefs: {},
  connected: false,
  events: [],
  conversationEvents: [],
  _commandBarPrefillHandler: null,

  setConnected: (v) => set({ connected: v }),
  selectMeeting: (id) => set({ currentMeetingId: id }),

  appendUserCommand: (text) => {
    const id = `cmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const ev: ConversationEvent = {
      id,
      ts: new Date().toISOString(),
      kind: "user_command",
      text,
      status: "pending",
    };
    set((s) => ({
      conversationEvents: [...s.conversationEvents.slice(-200), ev],
    }));
    return id;
  },
  appendAssistantReply: (text, kind = "assistant_reply", citations, status = "done") => {
    const id = `reply-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const ev: ConversationEvent = {
      id,
      ts: new Date().toISOString(),
      kind,
      text,
      citations,
      status,
    };
    set((s) => ({
      conversationEvents: [...s.conversationEvents.slice(-200), ev],
    }));
    return id;
  },
  patchConversationStatus: (id, status) =>
    set((s) => ({
      conversationEvents: s.conversationEvents.map((e) =>
        e.id === id ? { ...e, status } : e,
      ),
    })),
  patchAssistantReply: (id, patch) =>
    set((s) => ({
      conversationEvents: s.conversationEvents.map((e) =>
        e.id === id
          ? {
              ...e,
              ...(patch.text !== undefined ? { text: patch.text } : {}),
              ...(patch.citations !== undefined ? { citations: patch.citations } : {}),
              ...(patch.kind !== undefined ? { kind: patch.kind } : {}),
              ...(patch.status !== undefined ? { status: patch.status } : {}),
            }
          : e,
      ),
    })),
  clearConversationEvents: () => set({ conversationEvents: [] }),

  registerCommandBarPrefill: (handler) => {
    set({ _commandBarPrefillHandler: handler });
    return () => {
      if (get()._commandBarPrefillHandler === handler) {
        set({ _commandBarPrefillHandler: null });
      }
    };
  },

  prefillCommandBar: (text, meta) => {
    const h = get()._commandBarPrefillHandler;
    if (h) h(text, meta);
    // 无 handler 时静默：CommandBar 还没 mount（HMR 切换瞬间），下次再点会工作
  },

  reset: () =>
    set({
      meetings: {},
      currentMeetingId: null,
      meetingDetailLoaded: {},
      artifacts: [],
      failedArtifacts: [],
      pendingArtifactBriefs: {},
      events: [],
      conversationEvents: [],
    }),

  hydrateMeetings: (summaries) =>
    set((s) => {
      const next: Record<string, MeetingCard> = { ...s.meetings };
      for (const sum of summaries) {
        const cur = next[sum.meeting_id] ?? emptyMeeting(sum.meeting_id);
        // backend 状态三态 → 前端两态：finalized 视为 ended，保持已有 UI 颜色
        const uiState =
          sum.state === "in_meeting"
            ? "in_meeting"
            : "ended";
        next[sum.meeting_id] = {
          ...cur,
          // 已有非空 title 优先（事件流可能比 summary 含更新值如 minutes.title）
          title: cur.title && cur.title !== cur.meeting_id ? cur.title : (sum.title ?? cur.title),
          // M_minutes_refactor：display_title 一旦从后端拿到就持久化到 store
          display_title: sum.display_title ?? cur.display_title ?? null,
          state: uiState,
          started_at: cur.started_at ?? sum.started_at,
          ended_at: cur.ended_at ?? sum.ended_at ?? undefined,
        };
      }
      return { meetings: next };
    }),

  markMeetingDetailLoaded: (id) =>
    set((s) => ({
      meetingDetailLoaded: { ...s.meetingDetailLoaded, [id]: true },
    })),

  dismissFailedArtifact: (id) =>
    set((s) => ({
      failedArtifacts: s.failedArtifacts.filter((f) => f.id !== id),
    })),

  upsertMeeting: (id, patch) =>
    set((s) => {
      const cur = s.meetings[id] ?? emptyMeeting(id);
      return { meetings: { ...s.meetings, [id]: { ...cur, ...patch } } };
    }),

  addArtifact: (a) =>
    set((s) => {
      const dedup = s.artifacts.filter((x) => x.artifact_id !== a.artifact_id);
      return { artifacts: [a, ...dedup].slice(0, 50) };
    }),

  clearArtifacts: () => set({ artifacts: [] }),

  removeArtifact: (artifactId) =>
    set((s) => {
      const nextMeetings: Record<string, MeetingCard> = {};
      for (const [id, m] of Object.entries(s.meetings)) {
        nextMeetings[id] = {
          ...m,
          artifacts: m.artifacts.filter((x) => x.artifact_id !== artifactId),
        };
      }
      return {
        artifacts: s.artifacts.filter((x) => x.artifact_id !== artifactId),
        meetings: nextMeetings,
      };
    }),

  applyEvent: (e) => {
    set((s) => ({ events: [...s.events.slice(-200), e] }));

    const mid = e.meeting_id ?? undefined;
    if (mid && !get().meetings[mid]) {
      get().upsertMeeting(mid, { meeting_id: mid });
    }

    switch (e.type) {
      case "meeting.started":
        if (mid) {
          get().upsertMeeting(mid, {
            state: "in_meeting",
            started_at: e.ts,
          });
          // 总是把焦点切到最新启动的会议（demo 与真实开会都符合预期）
          set({ currentMeetingId: mid });
        }
        break;
      case "meeting.segment": {
        if (!mid) break;
        const seg = e.payload as unknown as TranscriptSegment;
        const cur = get().meetings[mid] ?? emptyMeeting(mid);
        const speakers = new Set(cur.speakers);
        if (seg.speaker_label) speakers.add(seg.speaker_label);
        get().upsertMeeting(mid, {
          segments: [...cur.segments, seg],
          speakers,
          state: "in_meeting",
        });
        break;
      }
      case "meeting.ended":
        if (mid)
          get().upsertMeeting(mid, {
            state: "ended",
            ended_at: e.ts,
            // 后端会紧跟着发 minutes.ready / minutes.failed，先把状态标为 generating
            // 避免短时间内 UI 显示「没有纪要」（in_meeting 文案）误导用户。
            // 已经有 minutes 的不覆盖（重试场景：先 ready 后 ended 不应回退）。
            minutes_status: get().meetings[mid]?.minutes
              ? "ok"
              : (get().meetings[mid]?.minutes_status ?? "generating"),
          });
        break;
      case "minutes.ready": {
        if (!mid) break;
        const m = e.payload as unknown as MeetingMinutes;
        get().upsertMeeting(mid, {
          minutes: m,
          title: m.title,
          // M_minutes_refactor：LLM 生成的 title 就是 display_title，同步给左侧列表
          display_title: m.title,
          state: "ended",
          minutes_status: "ok",
          minutes_error: null,
        });
        break;
      }
      case "meeting.todo.completed": {
        // M_minutes_refactor：artifact 生成完毕 → 后端回写完成事件 → 把对应 todo
        // status 置 done + artifact_id，避免必须等下次 GET /meetings/{id}/minutes
        // 才看到 checkbox 划掉的状态。
        if (!mid) break;
        const p = (e.payload ?? {}) as {
          todo_id?: string;
          artifact_id?: string;
          done_at?: string;
        };
        const cur = get().meetings[mid];
        if (!cur?.minutes || !p.todo_id) break;
        const todos = cur.minutes.todos ?? [];
        const next: TodoItem[] = todos.map((t) =>
          t.id === p.todo_id
            ? {
                ...t,
                status: "done",
                done_at: p.done_at ?? new Date().toISOString(),
                artifact_id: p.artifact_id ?? t.artifact_id ?? null,
              }
            : t,
        );
        get().upsertMeeting(mid, {
          minutes: { ...cur.minutes, todos: next },
        });
        break;
      }
      case "minutes.failed": {
        if (!mid) break;
        const p = (e.payload ?? {}) as { error?: string };
        get().upsertMeeting(mid, {
          state: "ended",
          minutes_status: "generation_failed",
          minutes_error: p.error ?? "未知错误",
        });
        break;
      }
      case "artifact.generating": {
        // 暂存 brief，方便 artifact.failed 回填用户原始命令；
        // 失败/成功后会被清除（见 artifact.failed / artifact.ready）。
        const p = (e.payload ?? {}) as { artifact_type?: string; brief?: string };
        if (p.artifact_type && typeof p.brief === "string" && p.brief) {
          set((s) => ({
            pendingArtifactBriefs: {
              ...s.pendingArtifactBriefs,
              [p.artifact_type as string]: p.brief as string,
            },
          }));
        }
        break;
      }
      case "artifact.ready": {
        const a = e.payload as unknown as GeneratedArtifact;
        get().addArtifact(a);
        if (mid) {
          const cur = get().meetings[mid];
          if (cur) {
            const dedup = cur.artifacts.filter(
              (x) => x.artifact_id !== a.artifact_id,
            );
            get().upsertMeeting(mid, { artifacts: [a, ...dedup] });
          }
        }
        // 配对的 brief 已经无用，清掉避免污染下一次失败回填。
        if (a?.artifact_type) {
          set((s) => {
            if (!(a.artifact_type in s.pendingArtifactBriefs)) return s;
            const next = { ...s.pendingArtifactBriefs };
            delete next[a.artifact_type];
            return { pendingArtifactBriefs: next };
          });
        }
        break;
      }
      case "artifact.failed": {
        const p = (e.payload ?? {}) as { artifact_type?: string };
        const briefs = get().pendingArtifactBriefs;
        const intentText = p.artifact_type ? briefs[p.artifact_type] : undefined;
        const failed = buildFailedArtifact(e, intentText);
        set((s) => {
          const nextBriefs = { ...s.pendingArtifactBriefs };
          if (p.artifact_type && p.artifact_type in nextBriefs) {
            delete nextBriefs[p.artifact_type];
          }
          return {
            failedArtifacts: [failed, ...s.failedArtifacts].slice(
              0,
              FAILED_ARTIFACT_LIMIT,
            ),
            pendingArtifactBriefs: nextBriefs,
          };
        });
        break;
      }
      default:
        break;
    }
  },
}));
