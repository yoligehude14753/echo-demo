import { create } from "zustand";
import type {
  EchoEvent,
  AgentTaskCard,
  AgentTaskEvent,
  GeneratedArtifact,
  MeetingCard,
  MeetingMinutes,
  TodoItem,
  TranscriptSegment,
  WorkflowRunDTO,
} from "@/types";
import type { MeetingSummary } from "@/api";
import {
  buildFailedArtifact,
  FAILED_ARTIFACT_LIMIT,
  type FailedArtifact,
} from "@/lib/failedArtifact";
import { shouldHideSharedPublicHistory } from "@/runtime";

export interface LocalAmbientSegment {
  text: string;
  captured_at: string;
  speaker_id: string | null;
  speaker_label: string | null;
  duration_ms: number;
}

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
  retry_of_run_id?: string;
}
export type CommandBarPrefillHandler = (
  text: string,
  meta?: CommandBarPrefillMeta,
) => void;

export type MeetingListLoadPhase =
  | "idle"
  | "loading"
  | "ready"
  | "error"
  | "degraded";

interface Store {
  meetings: Record<string, MeetingCard>;
  currentMeetingId: string | null;
  /**
   * 标记 meeting 详情已经从后端 detail endpoint（transcript/minutes/artifacts）
   * 拉过一次。避免每次切换都重复 fetch；新事件到达时（meeting.segment 等）store
   * 自然会通过 applyEvent 增量更新，无需重置该 flag。
   */
  meetingDetailLoaded: Record<string, boolean>;
  meetingDetailErrors: Record<string, string>;
  meetingDetailRetryRevision: Record<string, number>;
  meetingListLoadPhase: MeetingListLoadPhase;
  meetingListError: string | null;
  meetingListLastSuccessAt: string | null;
  meetingListRetryRevision: number;
  rehydrateRevision: number;
  rehydrateFenceSeq: number;
  meetingEventSeq: Record<string, number>;
  artifacts: GeneratedArtifact[];
  ambientSegments: LocalAmbientSegment[];
  failedArtifacts: FailedArtifact[];
  agentTasks: Record<string, AgentTaskCard>;
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
  rehydrateMeetings(summaries: MeetingSummary[], fenceSeq: number): void;
  requestRehydrate(fenceSeq?: number): void;
  /** 标记某 meeting detail 已加载完毕，避免重复 fetch。 */
  markMeetingDetailLoaded(id: string): void;
  markMeetingDetailError(id: string, message: string): void;
  retryMeetingDetail(id: string): void;
  startMeetingListLoad(): void;
  completeMeetingListLoad(): void;
  failMeetingListLoad(message: string): void;
  retryMeetingListLoad(): void;
  addArtifact(a: GeneratedArtifact): void;
  addAmbientSegment(seg: LocalAmbientSegment): void;
  markMeetingActive(
    meetingId: string,
    opts?: { title?: string | null; startedAt?: string | null; select?: boolean },
  ): void;
  markMeetingEnded(meetingId: string, endedAt?: string | null): void;
  addMeetingSegments(
    meetingId: string,
    segments: TranscriptSegment[],
    opts?: { startedAt?: string; select?: boolean },
  ): void;
  /**
   * 清空全局 outputs 列表（顶栏「清空」按钮）。
   * 不清 failedArtifacts —— 它们有独立 dismiss，避免一键覆盖失败上下文。
   * 也不清 meetings[*].artifacts —— 那是会议详情视图的快照，独立维护。
   */
  clearArtifacts(): void;
  /** 删除单条产物（hover × 按钮）。也同步从所有 meeting 的 artifacts 中清掉，避免悬挂引用。 */
  removeArtifact(artifactId: string): void;
  dismissFailedArtifact(id: string): void;
  upsertAgentTask(task: AgentTaskCard): void;
  applyAgentTaskEvent(event: AgentTaskEvent): void;
  /**
   * M_minutes_refactor：CommandBar 启动时注册 prefill handler；返回的 unregister
   * 可在 unmount 时调，避免 handler 引用陈旧实例（HMR 场景）。
   */
  registerCommandBarPrefill(handler: CommandBarPrefillHandler): () => void;
  /** 把 text 推给 CommandBar 预填（meta 透传，CommandBar 据此发 artifact 时附带 meeting_id/todo_id）。 */
  prefillCommandBar(text: string, meta?: CommandBarPrefillMeta): void;
  reset(): void;
}

const LOCAL_CAPTURE_STATE_KEY = "echodesk.localCaptureState.v1";
const LOCAL_LEGACY_IMPORT_KEY = "echodesk.localLegacyHistoryImport.v1";
const LOCAL_CAPTURE_STATE_SCHEMA = 1;
const MAX_PERSISTED_MEETINGS = 200;
const MAX_PERSISTED_SEGMENTS_PER_MEETING = 800;
const MAX_PERSISTED_AMBIENT = 120;
const MAX_PERSISTED_ARTIFACTS = 50;

interface PersistedMeetingCard
  extends Omit<MeetingCard, "speakers" | "segments" | "artifacts"> {
  segments: TranscriptSegment[];
  speakers: string[];
  artifacts: GeneratedArtifact[];
}

interface LocalCaptureStateSnapshot {
  schema: number;
  appVersion: string;
  savedAt: string;
  currentMeetingId: string | null;
  meetings: PersistedMeetingCard[];
  ambientSegments: LocalAmbientSegment[];
  artifacts: GeneratedArtifact[];
}

interface LegacyLocalHistorySnapshot extends LocalCaptureStateSnapshot {
  sourcePath?: string;
  sourceSize?: number;
  sourceMtimeMs?: number;
  importedAt?: string;
  meetingCount?: number;
  segmentCount?: number;
  error?: string;
}

function emptyMeeting(id: string, title?: string): MeetingCard {
  return {
    meeting_id: id,
    title: title ?? id,
    state: "idle",
    segments: [],
    speakers: new Set<string>(),
    summary_segment_count: 0,
    summary_speaker_count: 0,
    artifacts: [],
  };
}

export function projectTodoStatus(raw: unknown): TodoItem["status"] | null {
  if (raw === "succeeded" || raw === "done") return "done";
  if (raw === "running" || raw === "pending" || raw === "cancel_requested") {
    return "running";
  }
  if (raw === "failed" || raw === "timeout" || raw === "cancel_failed") {
    return "failed";
  }
  if (raw === "waiting_permission") return "waiting_permission";
  if (raw === "cancelled") return "cancelled";
  return null;
}

export function projectMinutesWithWorkflowRuns(
  minutes: MeetingMinutes | null | undefined,
  workflowRuns: WorkflowRunDTO[],
): MeetingMinutes | null | undefined {
  if (!minutes || workflowRuns.length === 0) return minutes;
  const latestByTodo = new Map<string, WorkflowRunDTO>();
  workflowRuns
    .slice()
    .sort((a, b) => a.updated_at.localeCompare(b.updated_at))
    .forEach((run) => {
      if (run.todo_id) latestByTodo.set(run.todo_id, run);
    });
  return {
    ...minutes,
    todos: minutes.todos.map((todo) => {
      const run = latestByTodo.get(todo.id);
      const status = projectTodoStatus(run?.state);
      if (!run || !status) return todo;
      const artifactId =
        typeof run.output?.artifact_id === "string"
          ? run.output.artifact_id
          : undefined;
      return {
        ...todo,
        status,
        workflow_run_id: run.run_id,
        artifact_id: artifactId ?? todo.artifact_id ?? null,
        done_at:
          status === "done"
            ? (todo.done_at ?? run.finished_at ?? new Date().toISOString())
            : todo.done_at,
      };
    }),
  };
}

function segmentKey(s: TranscriptSegment): string {
  return [
    s.start_ms,
    s.end_ms,
    s.text,
    s.speaker_id ?? "",
    s.speaker_label ?? "",
  ].join("\u0001");
}

function normalizeSegmentText(text: string): string {
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

function likelySameTranscript(a: string, b: string): boolean {
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

function isNearDuplicateSegment(
  existing: TranscriptSegment,
  incoming: TranscriptSegment,
): boolean {
  const a = normalizeSegmentText(existing.text);
  const b = normalizeSegmentText(incoming.text);
  if (!likelySameTranscript(a, b)) return false;
  const sameSpeaker =
    (existing.speaker_label ?? existing.speaker_id ?? "") ===
    (incoming.speaker_label ?? incoming.speaker_id ?? "");
  const startDelta = Math.abs((existing.start_ms ?? 0) - (incoming.start_ms ?? 0));
  const endDelta = Math.abs((existing.end_ms ?? 0) - (incoming.end_ms ?? 0));
  const near = startDelta <= 6_000 || endDelta <= 6_000;
  if (!near) return false;
  return a === b || sameSpeaker;
}

function mergeSegments(
  existing: TranscriptSegment[],
  incoming: TranscriptSegment[],
): TranscriptSegment[] {
  if (incoming.length === 0) return existing;
  const seen = new Set(existing.map(segmentKey));
  const merged = [...existing];
  for (const seg of incoming) {
    const key = segmentKey(seg);
    if (seen.has(key)) continue;
    if (merged.some((item) => isNearDuplicateSegment(item, seg))) continue;
    seen.add(key);
    merged.push(seg);
  }
  return merged.slice(-MAX_PERSISTED_SEGMENTS_PER_MEETING);
}

function speakerSetFromSegments(
  base: Set<string>,
  segments: TranscriptSegment[],
): Set<string> {
  const speakers = new Set(base);
  for (const seg of segments) {
    if (seg.speaker_label) speakers.add(seg.speaker_label);
  }
  return speakers;
}

function shouldPersistLocalCaptureState(): boolean {
  try {
    return shouldHideSharedPublicHistory();
  } catch {
    return false;
  }
}

function parseLocalCaptureSnapshot(raw: string | null): LocalCaptureStateSnapshot | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<LocalCaptureStateSnapshot>;
    if (parsed.schema !== LOCAL_CAPTURE_STATE_SCHEMA) return null;
    if (!Array.isArray(parsed.meetings)) return null;
    if (!Array.isArray(parsed.ambientSegments)) return null;
    if (!Array.isArray(parsed.artifacts)) return null;
    return parsed as LocalCaptureStateSnapshot;
  } catch {
    return null;
  }
}

function serializeMeeting(m: MeetingCard): PersistedMeetingCard {
  return {
    ...m,
    segments: m.segments.slice(-MAX_PERSISTED_SEGMENTS_PER_MEETING),
    speakers: Array.from(m.speakers),
    artifacts: m.artifacts.slice(0, MAX_PERSISTED_ARTIFACTS),
  };
}

export const useStore = create<Store>((set, get) => ({
  meetings: {},
  currentMeetingId: null,
  meetingDetailLoaded: {},
  meetingDetailErrors: {},
  meetingDetailRetryRevision: {},
  meetingListLoadPhase: "idle",
  meetingListError: null,
  meetingListLastSuccessAt: null,
  meetingListRetryRevision: 0,
  rehydrateRevision: 0,
  rehydrateFenceSeq: 0,
  meetingEventSeq: {},
  artifacts: [],
  ambientSegments: [],
  failedArtifacts: [],
  agentTasks: {},
  pendingArtifactBriefs: {},
  connected: false,
  events: [],
  _commandBarPrefillHandler: null,

  setConnected: (v) => set({ connected: v }),
  selectMeeting: (id) => set({ currentMeetingId: id }),

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
      meetingDetailErrors: {},
      meetingDetailRetryRevision: {},
      meetingListLoadPhase: "idle",
      meetingListError: null,
      meetingListLastSuccessAt: null,
      meetingListRetryRevision: 0,
      rehydrateRevision: 0,
      rehydrateFenceSeq: 0,
      meetingEventSeq: {},
      artifacts: [],
      ambientSegments: [],
      failedArtifacts: [],
      agentTasks: {},
      pendingArtifactBriefs: {},
      events: [],
    }),

  hydrateMeetings: (summaries) =>
    set((s) => {
      const next: Record<string, MeetingCard> = {};
      for (const sum of summaries) {
        const cur = s.meetings[sum.meeting_id] ?? emptyMeeting(sum.meeting_id);
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
          minutes_status:
            cur.minutes_status ?? (sum.has_minutes ? "ok" : undefined),
          summary_segment_count: Math.max(
            cur.summary_segment_count ?? 0,
            sum.n_segments,
          ),
          summary_speaker_count: Math.max(
            cur.summary_speaker_count ?? 0,
            sum.n_speakers,
          ),
        };
      }
      // The startup snapshot may have been requested before a local/manual start
      // completed. Never let that older empty snapshot erase an in-progress card;
      // the next server event or resync will reconcile it by sequence.
      for (const [id, meeting] of Object.entries(s.meetings)) {
        if (!next[id] && meeting.state === "in_meeting") {
          next[id] = meeting;
        }
      }
      return {
        meetings: next,
        currentMeetingId:
          s.currentMeetingId && next[s.currentMeetingId]
            ? s.currentMeetingId
            : null,
      };
    }),

  rehydrateMeetings: (summaries, fenceSeq) =>
    set((s) => {
      const next: Record<string, MeetingCard> = {};
      for (const sum of summaries) {
        const cur = s.meetings[sum.meeting_id] ?? emptyMeeting(sum.meeting_id);
        if ((s.meetingEventSeq[sum.meeting_id] ?? 0) > fenceSeq) {
          next[sum.meeting_id] = cur;
          continue;
        }
        next[sum.meeting_id] = {
          ...cur,
          title: sum.display_title ?? sum.title ?? cur.title,
          display_title: sum.display_title ?? cur.display_title ?? null,
          state: sum.state === "in_meeting" ? "in_meeting" : "ended",
          started_at: sum.started_at,
          ended_at: sum.ended_at ?? undefined,
          summary_segment_count: sum.n_segments,
          summary_speaker_count: sum.n_speakers,
          minutes_status: sum.has_minutes ? "ok" : cur.minutes_status,
        };
      }
      for (const [id, meeting] of Object.entries(s.meetings)) {
        if (
          !next[id] &&
          (meeting.state === "in_meeting" ||
            (s.meetingEventSeq[id] ?? 0) > fenceSeq)
        ) {
          next[id] = meeting;
        }
      }
      return {
        meetings: next,
        meetingDetailLoaded: {},
        meetingDetailErrors: {},
        currentMeetingId:
          s.currentMeetingId && next[s.currentMeetingId]
            ? s.currentMeetingId
            : null,
      };
    }),

  requestRehydrate: (fenceSeq) =>
    set((s) => ({
      rehydrateRevision: s.rehydrateRevision + 1,
      rehydrateFenceSeq:
        fenceSeq ??
        s.events.reduce((max, event) => Math.max(max, event.seq ?? 0), 0),
      meetingDetailLoaded: {},
      meetingDetailErrors: {},
    })),

  markMeetingDetailLoaded: (id) =>
    set((s) => {
      const meetingDetailErrors = { ...s.meetingDetailErrors };
      delete meetingDetailErrors[id];
      return {
        meetingDetailLoaded: { ...s.meetingDetailLoaded, [id]: true },
        meetingDetailErrors,
      };
    }),

  markMeetingDetailError: (id, message) =>
    set((s) => ({
      meetingDetailErrors: { ...s.meetingDetailErrors, [id]: message },
    })),

  retryMeetingDetail: (id) =>
    set((s) => {
      const meetingDetailErrors = { ...s.meetingDetailErrors };
      delete meetingDetailErrors[id];
      return {
        meetingDetailErrors,
        meetingDetailRetryRevision: {
          ...s.meetingDetailRetryRevision,
          [id]: (s.meetingDetailRetryRevision[id] ?? 0) + 1,
        },
      };
    }),

  startMeetingListLoad: () =>
    set({
      meetingListLoadPhase: "loading",
      meetingListError: null,
    }),

  completeMeetingListLoad: () =>
    set({
      meetingListLoadPhase: "ready",
      meetingListError: null,
      meetingListLastSuccessAt: new Date().toISOString(),
    }),

  failMeetingListLoad: (message) =>
    set((s) => ({
      meetingListLoadPhase:
        Object.keys(s.meetings).length > 0 ? "degraded" : "error",
      meetingListError: message,
    })),

  retryMeetingListLoad: () =>
    set((s) => ({
      meetingListLoadPhase: "loading",
      meetingListError: null,
      meetingListRetryRevision: s.meetingListRetryRevision + 1,
    })),

  dismissFailedArtifact: (id) =>
    set((s) => ({
      failedArtifacts: s.failedArtifacts.filter((f) => f.id !== id),
    })),

  upsertAgentTask: (task) =>
    set((s) => ({
      agentTasks: {
        ...s.agentTasks,
        [task.task_id]: task,
      },
    })),

  applyAgentTaskEvent: (event) =>
    set((s) => {
      if (event.visibility !== "user") return s;
      const prev = s.agentTasks[event.task_id];
      const snap = event.snapshot ?? {};
      const eventArtifacts =
        Array.isArray(event.artifacts) && event.artifacts.length > 0
          ? event.artifacts
          : null;
      const next: AgentTaskCard = {
        task_id: event.task_id,
        runner_task_id: event.runner_task_id ?? prev?.runner_task_id ?? null,
        device_id: prev?.device_id ?? "desktop",
        conversation_id: event.conversation_id ?? prev?.conversation_id ?? null,
        message_id: event.message_id ?? prev?.message_id ?? null,
        title: String(snap.title ?? event.title ?? prev?.title ?? "EchoDesk 正在执行"),
        intent_text: prev?.intent_text ?? String(snap.title ?? event.title ?? ""),
        route: prev?.route ?? "agent",
        task_kind: prev?.task_kind ?? "agent_task",
        state: event.state,
        progress_text: String(snap.progress_text ?? event.message ?? prev?.progress_text ?? ""),
        final_text:
          typeof snap.final_text === "string"
            ? snap.final_text
            : (event.message ?? prev?.final_text ?? null),
        error:
          typeof snap.error === "string"
            ? snap.error
            : (event.event.includes("failed") ? event.message ?? prev?.error ?? null : prev?.error ?? null),
        artifacts: Array.isArray(snap.artifacts) ? snap.artifacts : (eventArtifacts ?? prev?.artifacts ?? []),
        snapshot: snap,
        last_seq: event.seq,
        submitted_at: prev?.submitted_at ?? event.ts,
        finished_at:
          ["succeeded", "failed", "cancelled", "cancel_failed", "timeout"].includes(event.state)
            ? event.ts
            : (prev?.finished_at ?? null),
        timeout_s: prev?.timeout_s ?? 1800,
      };
      return {
        agentTasks: {
          ...s.agentTasks,
          [event.task_id]: next,
        },
      };
    }),

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

  addAmbientSegment: (seg) =>
    set((s) => ({
      ambientSegments: [...s.ambientSegments, seg].slice(-120),
    })),

  markMeetingActive: (meetingId, opts) =>
    set((s) => {
      const cur = s.meetings[meetingId] ?? emptyMeeting(meetingId, opts?.title ?? undefined);
      return {
        currentMeetingId: opts?.select ? meetingId : s.currentMeetingId,
        meetings: {
          ...s.meetings,
          [meetingId]: {
            ...cur,
            title: opts?.title || cur.title || meetingId,
            state: "in_meeting",
            started_at:
              cur.started_at ?? opts?.startedAt ?? new Date().toISOString(),
          },
        },
      };
    }),

  markMeetingEnded: (meetingId, endedAt) =>
    set((s) => {
      const cur = s.meetings[meetingId] ?? emptyMeeting(meetingId);
      return {
        meetings: {
          ...s.meetings,
          [meetingId]: {
            ...cur,
            state: "ended",
            ended_at: endedAt ?? new Date().toISOString(),
            minutes_status: cur.minutes
              ? "ok"
              : (cur.minutes_status ?? "generating"),
          },
        },
      };
    }),

  addMeetingSegments: (meetingId, segments, opts) =>
    set((s) => {
      const cur = s.meetings[meetingId] ?? emptyMeeting(meetingId);
      const mergedSegments = mergeSegments(cur.segments, segments);
      const speakers = speakerSetFromSegments(cur.speakers, mergedSegments);
      return {
        currentMeetingId: opts?.select ? meetingId : s.currentMeetingId,
        meetings: {
          ...s.meetings,
          [meetingId]: {
            ...cur,
            state: cur.state === "ended" ? "ended" : "in_meeting",
            started_at:
              cur.started_at ?? opts?.startedAt ?? new Date().toISOString(),
            segments: mergedSegments,
            speakers,
            summary_segment_count: Math.max(
              cur.summary_segment_count ?? 0,
              mergedSegments.length,
            ),
            summary_speaker_count: Math.max(
              cur.summary_speaker_count ?? 0,
              speakers.size,
            ),
          },
        },
      };
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
    set((s) => ({
      events: [...s.events.slice(-200), e],
      meetingEventSeq: e.meeting_id
        ? {
            ...s.meetingEventSeq,
            [e.meeting_id]: Math.max(
              s.meetingEventSeq[e.meeting_id] ?? 0,
              e.seq ?? 0,
            ),
          }
        : s.meetingEventSeq,
    }));

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
        const mergedSegments = mergeSegments(cur.segments, [seg]);
        const speakers = speakerSetFromSegments(cur.speakers, mergedSegments);
        get().upsertMeeting(mid, {
          segments: mergedSegments,
          speakers,
          summary_segment_count: Math.max(
            cur.summary_segment_count ?? 0,
            mergedSegments.length,
          ),
          summary_speaker_count: Math.max(
            cur.summary_speaker_count ?? 0,
            speakers.size,
          ),
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
      case "meeting.todo.updated": {
        if (!mid) break;
        const p = (e.payload ?? {}) as {
          todo_id?: string;
          status?: TodoItem["status"];
          state?: string;
          run_id?: string;
          artifact_id?: string;
          done_at?: string;
        };
        const cur = get().meetings[mid];
        if (!cur?.minutes || !p.todo_id) break;
        const projected = projectTodoStatus(p.status ?? p.state);
        if (!projected) break;
        const next: TodoItem[] = cur.minutes.todos.map((t) =>
          t.id === p.todo_id
            ? {
                ...t,
                status: projected,
                workflow_run_id: p.run_id ?? t.workflow_run_id ?? null,
                done_at:
                  projected === "done"
                    ? (p.done_at ?? t.done_at ?? new Date().toISOString())
                    : t.done_at,
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
        const p = (e.payload ?? {}) as { artifact_type?: string; brief?: string; run_id?: string };
        if (p.artifact_type && typeof p.brief === "string" && p.brief) {
          set((s) => ({
            pendingArtifactBriefs: {
              ...s.pendingArtifactBriefs,
              [p.artifact_type as string]: p.brief as string,
              ...(p.run_id ? { [p.run_id]: p.brief as string } : {}),
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
        if (a?.artifact_type || a?.run_id) {
          set((s) => {
            const next = { ...s.pendingArtifactBriefs };
            if (a.artifact_type) delete next[a.artifact_type];
            if (a.run_id) delete next[a.run_id];
            return { pendingArtifactBriefs: next };
          });
        }
        break;
      }
      case "artifact.failed": {
        const p = (e.payload ?? {}) as { artifact_type?: string; run_id?: string };
        const briefs = get().pendingArtifactBriefs;
        const intentText =
          (p.run_id ? briefs[p.run_id] : undefined) ??
          (p.artifact_type ? briefs[p.artifact_type] : undefined);
        const failed = buildFailedArtifact(e, intentText);
        set((s) => {
          const nextBriefs = { ...s.pendingArtifactBriefs };
          if (p.artifact_type && p.artifact_type in nextBriefs) {
            delete nextBriefs[p.artifact_type];
          }
          if (p.run_id && p.run_id in nextBriefs) {
            delete nextBriefs[p.run_id];
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
      case "workflow.snapshot": {
        const run = e.payload as unknown as WorkflowRunDTO;
        if (!run.meeting_id || !run.todo_id) break;
        const cur = get().meetings[run.meeting_id];
        if (!cur?.minutes) break;
        const projected = projectTodoStatus(run.state);
        if (!projected) break;
        const artifactId =
          typeof run.output?.artifact_id === "string"
            ? run.output.artifact_id
            : undefined;
        const next: TodoItem[] = cur.minutes.todos.map((t) =>
          t.id === run.todo_id
            ? {
                ...t,
                status: projected,
                workflow_run_id: run.run_id,
                artifact_id: artifactId ?? t.artifact_id ?? null,
                done_at:
                  projected === "done"
                    ? (t.done_at ?? run.finished_at ?? new Date().toISOString())
                    : t.done_at,
              }
            : t,
        );
        get().upsertMeeting(run.meeting_id, {
          minutes: { ...cur.minutes, todos: next },
        });
        break;
      }
      case "agent.task.event": {
        get().applyAgentTaskEvent(e.payload as unknown as AgentTaskEvent);
        break;
      }
      default:
        break;
    }
  },
}));

let localCapturePersistenceInstalled = false;
let localCapturePersistTimer: number | null = null;

function writeLocalCaptureSnapshot(state: Store): void {
  if (typeof window === "undefined") return;
  if (!shouldPersistLocalCaptureState()) return;
  try {
    const meetings = Object.values(state.meetings)
      .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))
      .slice(0, MAX_PERSISTED_MEETINGS)
      .map(serializeMeeting);
    const snapshot: LocalCaptureStateSnapshot = {
      schema: LOCAL_CAPTURE_STATE_SCHEMA,
      appVersion:
        typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "unknown",
      savedAt: new Date().toISOString(),
      currentMeetingId:
        state.currentMeetingId && state.meetings[state.currentMeetingId]
          ? state.currentMeetingId
          : null,
      meetings,
      ambientSegments: state.ambientSegments.slice(-MAX_PERSISTED_AMBIENT),
      artifacts: state.artifacts.slice(0, MAX_PERSISTED_ARTIFACTS),
    };
    window.localStorage.setItem(
      LOCAL_CAPTURE_STATE_KEY,
      JSON.stringify(snapshot),
    );
  } catch {
    // localStorage 写满或 WebView 禁用时不阻塞主链路。
  }
}

function scheduleLocalCapturePersist(): void {
  if (typeof window === "undefined") return;
  if (!shouldPersistLocalCaptureState()) return;
  if (localCapturePersistTimer) window.clearTimeout(localCapturePersistTimer);
  localCapturePersistTimer = window.setTimeout(() => {
    localCapturePersistTimer = null;
    writeLocalCaptureSnapshot(useStore.getState());
  }, 150);
}

function hydrateLocalCaptureSnapshot(): void {
  if (typeof window === "undefined") return;
  if (!shouldPersistLocalCaptureState()) return;
  const snapshot = parseLocalCaptureSnapshot(
    window.localStorage.getItem(LOCAL_CAPTURE_STATE_KEY),
  );
  if (!snapshot) return;
  const meetings: Record<string, MeetingCard> = {};
  for (const persisted of snapshot.meetings.slice(0, MAX_PERSISTED_MEETINGS)) {
    if (!persisted?.meeting_id) continue;
    const segments = (persisted.segments ?? []).slice(
      -MAX_PERSISTED_SEGMENTS_PER_MEETING,
    );
    if (persisted.state === "in_meeting" && segments.length === 0) {
      continue;
    }
    meetings[persisted.meeting_id] = {
      ...persisted,
      state: persisted.state === "in_meeting" ? "ended" : persisted.state,
      ended_at:
        persisted.state === "in_meeting"
          ? (persisted.ended_at ?? snapshot.savedAt)
          : persisted.ended_at,
      segments,
      speakers: new Set(persisted.speakers ?? []),
      summary_segment_count:
        persisted.summary_segment_count ?? segments.length,
      summary_speaker_count:
        persisted.summary_speaker_count ?? new Set(persisted.speakers ?? []).size,
      artifacts: persisted.artifacts ?? [],
    };
  }
  const snapshotCurrent =
    snapshot.currentMeetingId && meetings[snapshot.currentMeetingId]
      ? meetings[snapshot.currentMeetingId]
      : null;
  useStore.setState((s) => ({
    meetings: { ...meetings, ...s.meetings },
    currentMeetingId:
      snapshot.currentMeetingId &&
      snapshotCurrent &&
      snapshotCurrent.state !== "in_meeting"
        ? snapshot.currentMeetingId
        : s.currentMeetingId,
    ambientSegments:
      s.ambientSegments.length > 0
        ? s.ambientSegments
        : snapshot.ambientSegments.slice(-MAX_PERSISTED_AMBIENT),
    artifacts:
      s.artifacts.length > 0
        ? s.artifacts
        : snapshot.artifacts.slice(0, MAX_PERSISTED_ARTIFACTS),
  }));
}

function importFingerprint(snapshot: LegacyLocalHistorySnapshot): string {
  return [
    snapshot.sourcePath ?? "",
    snapshot.sourceSize ?? 0,
    Math.round(snapshot.sourceMtimeMs ?? 0),
    snapshot.meetingCount ?? snapshot.meetings.length,
    snapshot.segmentCount ?? 0,
  ].join("|");
}

function snapshotToMeetings(
  snapshot: LocalCaptureStateSnapshot,
): Record<string, MeetingCard> {
  const meetings: Record<string, MeetingCard> = {};
  for (const persisted of snapshot.meetings.slice(0, MAX_PERSISTED_MEETINGS)) {
    if (!persisted?.meeting_id) continue;
    const segments = (persisted.segments ?? []).slice(
      -MAX_PERSISTED_SEGMENTS_PER_MEETING,
    );
    if (persisted.state === "in_meeting" && segments.length === 0) {
      continue;
    }
    meetings[persisted.meeting_id] = {
      ...persisted,
      state: persisted.state === "in_meeting" ? "ended" : persisted.state,
      ended_at:
        persisted.state === "in_meeting"
          ? (persisted.ended_at ?? snapshot.savedAt)
          : persisted.ended_at,
      segments,
      speakers: new Set(persisted.speakers ?? []),
      summary_segment_count:
        persisted.summary_segment_count ?? segments.length,
      summary_speaker_count:
        persisted.summary_speaker_count ?? new Set(persisted.speakers ?? []).size,
      artifacts: persisted.artifacts ?? [],
    };
  }
  return meetings;
}

async function hydrateLegacyLocalHistory(): Promise<void> {
  if (typeof window === "undefined") return;
  if (!shouldPersistLocalCaptureState()) return;
  const loader = window.echo?.loadLocalLegacyHistory;
  if (!loader) return;
  try {
    const snapshot = (await loader()) as LegacyLocalHistorySnapshot | null;
    if (!snapshot || snapshot.error || !Array.isArray(snapshot.meetings)) return;
    if (snapshot.meetings.length === 0 && snapshot.ambientSegments.length === 0) {
      return;
    }

    const fingerprint = importFingerprint(snapshot);
    const currentRaw = window.localStorage.getItem(LOCAL_CAPTURE_STATE_KEY);
    const currentSnapshot = parseLocalCaptureSnapshot(currentRaw);
    const markerRaw = window.localStorage.getItem(LOCAL_LEGACY_IMPORT_KEY);
    if (markerRaw) {
      try {
        const marker = JSON.parse(markerRaw) as {
          fingerprint?: string;
          meetingCount?: number;
        };
        if (
          marker.fingerprint === fingerprint &&
          (currentSnapshot?.meetings.length ?? 0) >=
            (marker.meetingCount ?? snapshot.meetings.length)
        ) {
          return;
        }
      } catch {
        // marker 损坏时重新导入；merge 是幂等的。
      }
    }

    const legacyMeetings = snapshotToMeetings(snapshot);
    const legacyDetailLoaded = Object.fromEntries(
      Object.keys(legacyMeetings).map((id) => [id, true]),
    );
    useStore.setState((s) => ({
      meetings: { ...legacyMeetings, ...s.meetings },
      meetingDetailLoaded: {
        ...legacyDetailLoaded,
        ...s.meetingDetailLoaded,
      },
      currentMeetingId:
        s.currentMeetingId && s.meetings[s.currentMeetingId]
          ? s.currentMeetingId
          : null,
      ambientSegments:
        s.ambientSegments.length > 0
          ? s.ambientSegments
          : snapshot.ambientSegments.slice(-MAX_PERSISTED_AMBIENT),
      artifacts:
        s.artifacts.length > 0
          ? s.artifacts
          : snapshot.artifacts.slice(0, MAX_PERSISTED_ARTIFACTS),
    }));
    writeLocalCaptureSnapshot(useStore.getState());
    window.localStorage.setItem(
      LOCAL_LEGACY_IMPORT_KEY,
      JSON.stringify({
        fingerprint,
        sourcePath: snapshot.sourcePath ?? null,
        meetingCount: snapshot.meetings.length,
        segmentCount: snapshot.segmentCount ?? null,
        importedAt: new Date().toISOString(),
      }),
    );
  } catch (e) {
    console.warn("[legacy-history] local import failed:", e);
  }
}

/**
 * Public demo / Android TV 不读取共享 backend 历史，因此本机采集出的会议和
 * ambient 片段必须落到 localStorage。该持久化只在 shouldHideSharedPublicHistory()
 * 为 true 时生效；用户配置私有 backend 后仍以私有后端 DB 为真相源。
 */
export function installLocalCapturePersistence(): void {
  if (localCapturePersistenceInstalled) return;
  localCapturePersistenceInstalled = true;
  hydrateLocalCaptureSnapshot();
  void hydrateLegacyLocalHistory();
  useStore.subscribe(() => scheduleLocalCapturePersist());
}

export const __LOCAL_CAPTURE_STATE_KEY_FOR_TEST__ = LOCAL_CAPTURE_STATE_KEY;
