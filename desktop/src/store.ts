import { create } from "zustand";
import type {
  EchoEvent,
  GeneratedArtifact,
  MeetingCard,
  MeetingMinutes,
  TranscriptSegment,
} from "@/types";
import type { MeetingSummary } from "@/api";
import {
  buildFailedArtifact,
  FAILED_ARTIFACT_LIMIT,
  type FailedArtifact,
} from "@/lib/failedArtifact";

interface Store {
  meetings: Record<string, MeetingCard>;
  currentMeetingId: string | null;
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

  setConnected: (v) => set({ connected: v }),
  selectMeeting: (id) => set({ currentMeetingId: id }),

  reset: () =>
    set({
      meetings: {},
      currentMeetingId: null,
      meetingDetailLoaded: {},
      artifacts: [],
      failedArtifacts: [],
      pendingArtifactBriefs: {},
      events: [],
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
          });
        break;
      case "minutes.ready": {
        if (!mid) break;
        const m = e.payload as unknown as MeetingMinutes;
        get().upsertMeeting(mid, {
          minutes: m,
          title: m.title,
          state: "ended",
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
