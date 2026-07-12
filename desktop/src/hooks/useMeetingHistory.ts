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
 * 4. **显式降级**：列表失败保留已有 in-memory 会议，并在列表呈现 error/degraded
 *    与重试入口；详情失败保留卡片级重试。错误仍写 console.warn 便于诊断。
 */

import { useEffect, useRef } from "react";
import {
  getMeetingArtifacts,
  getMeetingMinutes,
  getMeetingTranscript,
  listMeetings,
  listWorkflowRuns,
} from "@/api";
import { projectMinutesWithWorkflowRuns, useStore } from "@/store";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";

export function useMeetingHistory(): void {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const hydrateMeetings = useStore((s) => s.hydrateMeetings);
  const rehydrateMeetings = useStore((s) => s.rehydrateMeetings);
  const rehydrateRevision = useStore((s) => s.rehydrateRevision);
  const rehydrateFenceSeq = useStore((s) => s.rehydrateFenceSeq);
  const upsertMeeting = useStore((s) => s.upsertMeeting);
  const applyEvent = useStore((s) => s.applyEvent);
  const markDetailLoaded = useStore((s) => s.markMeetingDetailLoaded);
  const markDetailError = useStore((s) => s.markMeetingDetailError);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meetingListRetryRevision = useStore((s) => s.meetingListRetryRevision);
  const startMeetingListLoad = useStore((s) => s.startMeetingListLoad);
  const completeMeetingListLoad = useStore((s) => s.completeMeetingListLoad);
  const failMeetingListLoad = useStore((s) => s.failMeetingListLoad);
  const detailRetryRevision = useStore((s) =>
    currentMeetingId
      ? (s.meetingDetailRetryRevision[currentMeetingId] ?? 0)
      : 0,
  );
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

  // 启动期 hydrate：指数退避重试，覆盖"backend 比 renderer 晚 5-10s 起来"
  // 这一段 race —— 之前是单次失败永不重试，导致 swap app 后用户看到"历史记录
  // 又丢了"（其实 DB 44 条都在，纯前端 listMeetings 命中 backend 启动窗口）。
  // 退避序列 300ms / 800ms / 2s / 5s / 10s，总 ~18s 覆盖 cold start。
  // 任一次 200 OK 立即停；alive 守护 unmount 时早退。
  useEffect(() => {
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && isCurrent(originGeneration) && !controller.signal.aborted;
    startMeetingListLoad();
    const fenceSeq =
      rehydrateRevision > 0
        ? rehydrateFenceSeq
        : useStore
            .getState()
            .events.reduce((max, event) => Math.max(max, event.seq ?? 0), 0);
    const delays = [0, 300, 800, 2000, 5000, 10_000];
    void (async (): Promise<void> => {
      for (let i = 0; i < delays.length && canCommit(); i++) {
        if (delays[i] > 0) {
          await new Promise<void>((res) => setTimeout(res, delays[i]));
        }
        if (!canCommit()) return;
        try {
          const list = await listMeetings(50, { signal: controller.signal });
          if (!canCommit()) return;
          if (rehydrateRevision > 0) {
            rehydrateMeetings(list, fenceSeq);
          } else {
            hydrateMeetings(list);
          }
          completeMeetingListLoad();
          return; // 成功立即终止
        } catch (e) {
          if (!canCommit()) return;
          if (i === delays.length - 1) {
            console.warn(
              "[meeting-history] listMeetings failed after retries:",
              e,
            );
            failMeetingListLoad("历史会议暂时无法加载，请检查服务连接后重试");
          }
        }
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [
    backendOriginRevision,
    captureGeneration,
    hydrateMeetings,
    rehydrateMeetings,
    rehydrateRevision,
    rehydrateFenceSeq,
    meetingListRetryRevision,
    startMeetingListLoad,
    completeMeetingListLoad,
    failMeetingListLoad,
    isCurrent,
    registerAbortController,
  ]);

  // 选中后按需拉 detail
  useEffect(() => {
    if (!currentMeetingId) return;
    if (detailLoadedRef.current[currentMeetingId]) return;
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && isCurrent(originGeneration) && !controller.signal.aborted;
    void (async (): Promise<void> => {
      try {
        const [segs, minutes, arts, workflowRuns] = await Promise.all([
          getMeetingTranscript(currentMeetingId, { signal: controller.signal }),
          getMeetingMinutes(currentMeetingId, { signal: controller.signal }),
          getMeetingArtifacts(currentMeetingId, { signal: controller.signal }),
          listWorkflowRuns(
            { meeting_id: currentMeetingId, limit: 100 },
            { signal: controller.signal },
          ),
        ]);
        if (!canCommit()) return;
        const cur = meetingsRef.current[currentMeetingId];
        const restoredMinutes = projectMinutesWithWorkflowRuns(
          cur?.minutes ?? minutes,
          workflowRuns,
        );
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
          minutes: restoredMinutes ?? undefined,
          // backend 当前总返回 []，未来接 DB join 后这里就生效；in-memory artifacts 不会被空数组覆盖。
          artifacts: arts.length > 0 ? arts : (cur?.artifacts ?? []),
        });
        workflowRuns
          .slice()
          .sort((a, b) => a.updated_at.localeCompare(b.updated_at))
          .forEach((run) => {
            applyEvent({
              type: "workflow.snapshot",
              seq: 0,
              ts: run.updated_at,
              meeting_id: run.meeting_id,
              payload: run as unknown as Record<string, unknown>,
            });
          });
        markDetailLoaded(currentMeetingId);
      } catch (e) {
        if (!canCommit()) return;
        console.warn("[meeting-history] load detail failed:", e);
        markDetailError(currentMeetingId, "会议详情加载失败 · 点击重试");
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [
    applyEvent,
    backendOriginRevision,
    captureGeneration,
    currentMeetingId,
    detailRetryRevision,
    upsertMeeting,
    markDetailLoaded,
    markDetailError,
    isCurrent,
    registerAbortController,
  ]);
}
