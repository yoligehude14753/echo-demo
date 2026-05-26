import { create } from "zustand";
import type {
  EchoEvent,
  GeneratedArtifact,
  MeetingCard,
  MeetingMinutes,
  TranscriptSegment,
} from "@/types";

interface Store {
  meetings: Record<string, MeetingCard>;
  currentMeetingId: string | null;
  artifacts: GeneratedArtifact[];
  connected: boolean;
  events: EchoEvent[];

  setConnected(v: boolean): void;
  selectMeeting(id: string | null): void;
  applyEvent(e: EchoEvent): void;
  upsertMeeting(id: string, patch: Partial<MeetingCard>): void;
  addArtifact(a: GeneratedArtifact): void;
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
  artifacts: [],
  connected: false,
  events: [],

  setConnected: (v) => set({ connected: v }),
  selectMeeting: (id) => set({ currentMeetingId: id }),

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
        break;
      }
      default:
        break;
    }
  },
}));
