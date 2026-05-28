/**
 * useMeetingHistory：左侧会议列表 + 中右面板联动的"启动期 hydrate"和"按需 fetch"。
 *
 * 设计要点（P4 M_meeting_history）：
 *
 * 1. **启动期 hydrate**：App mount 时调一次 GET /meetings，把所有历史会议合并到
 *    store。WS replay buffer 可能漏掉早期事件，DB 是唯一真相源。
 *
 * 2. **按需 fetch detail**：当 currentMeetingId 变化、且对应 meeting 没标记为
 *    "detail loaded" 时，并发拉 transcript / minutes / artifacts，把结果合并进
 *    store.meetings[id]。已有事件流注入的 segments / minutes 不会被覆盖（用
 *    "数量更多者胜出"作为同步策略）。
 *
 * 3. **特殊 id 不 fetch**：currentMeetingId === null（虚拟"待机时段"）跳过
 *    detail fetch；该状态下 TranscriptStream 显示 ambient feed，MinutesView /
 *    ArtifactPanel 显示空态。
 *
 * 4. **错误吞掉**：列表/详情 fetch 失败不 toast 不抛错；前端继续用事件流维护
 *    in-memory 视图（容错降级）。错误打 console.warn 便于调试。
 */

import { useEffect, useRef } from "react";
import {
  getMeetingArtifacts,
  getMeetingMinutes,
  getMeetingTranscript,
  listMeetings,
} from "@/api";
import { useStore } from "@/store";

export function useMeetingHistory(): void {
  const hydrateMeetings = useStore((s) => s.hydrateMeetings);
  const upsertMeeting = useStore((s) => s.upsertMeeting);
  const markDetailLoaded = useStore((s) => s.markMeetingDetailLoaded);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  // 用 ref 避开 selector 依赖闭包：currentMeetingId 频繁变化但我们只需当下读 1 次
  const detailLoadedRef = useRef<Record<string, boolean>>({});
  const meetingsRef = useRef<ReturnType<typeof useStore.getState>["meetings"]>(
    {},
  );

  useEffect(() => {
    const unsub = useStore.subscribe((s) => {
      detailLoadedRef.current = s.meetingDetailLoaded;
      meetingsRef.current = s.meetings;
    });
    detailLoadedRef.current = useStore.getState().meetingDetailLoaded;
    meetingsRef.current = useStore.getState().meetings;
    return unsub;
  }, []);

  // 启动期一次性 hydrate；不重试（断网时事件流接管）
  useEffect(() => {
    let alive = true;
    void (async (): Promise<void> => {
      try {
        const list = await listMeetings(50);
        if (!alive) return;
        hydrateMeetings(list);
      } catch (e) {
        console.warn("[meeting-history] listMeetings failed:", e);
      }
    })();
    return () => {
      alive = false;
    };
  }, [hydrateMeetings]);

  // 选中后按需拉 detail
  useEffect(() => {
    if (!currentMeetingId) return;
    if (detailLoadedRef.current[currentMeetingId]) return;
    let alive = true;
    void (async (): Promise<void> => {
      try {
        const [segs, minutes, arts] = await Promise.all([
          getMeetingTranscript(currentMeetingId).catch(() => []),
          getMeetingMinutes(currentMeetingId).catch(() => null),
          getMeetingArtifacts(currentMeetingId).catch(() => []),
        ]);
        if (!alive) return;
        const cur = meetingsRef.current[currentMeetingId];
        // 合并策略：DB 段更多就用 DB；事件流段更多（in-progress）就保留事件流。
        // 避免 detail fetch 在会议进行中覆盖掉新到的 ws segment。
        const mergedSegments =
          cur && cur.segments.length > segs.length ? cur.segments : segs;
        const speakers = new Set<string>();
        for (const s of mergedSegments) {
          if (s.speaker_label) speakers.add(s.speaker_label);
        }
        upsertMeeting(currentMeetingId, {
          segments: mergedSegments,
          speakers,
          minutes: cur?.minutes ?? minutes ?? undefined,
          // backend 当前总返回 []，未来接 DB join 后这里就生效；in-memory artifacts 不会被空数组覆盖。
          artifacts: arts.length > 0 ? arts : (cur?.artifacts ?? []),
        });
        markDetailLoaded(currentMeetingId);
      } catch (e) {
        console.warn("[meeting-history] load detail failed:", e);
      }
    })();
    return () => {
      alive = false;
    };
  }, [currentMeetingId, upsertMeeting, markDetailLoaded]);
}
