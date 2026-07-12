import { Empty } from "antd";
import { AlertTriangle, LoaderCircle, Radio, RefreshCw, Search } from "lucide-react";
import { useState } from "react";
import { useStore } from "@/store";
import { countDisplaySpeakers } from "@/lib/speakerDisplay";
import { meetingDisplayTitle } from "@/lib/meetingDisplay";
import type { CaptureState } from "@/domain/session";
import type { MeetingCard } from "@/types";

const dot: Record<MeetingCard["state"], string> = {
  idle: "bg-ink-400",
  in_meeting: "bg-accent animate-pulse",
  ended: "bg-ink-500",
};

const label: Record<MeetingCard["state"], string> = {
  idle: "待开始",
  in_meeting: "进行中",
  ended: "已结束",
};

/**
 * 列表第一项是虚拟"实时记录"——非会议状态的 ambient 转写聚合。
 *
 * 产品决策（2026-05-28）：ambient_segments 表没有 meeting_id 列，所有非会议
 * 时段的语音段共用一个全局 lane。把它当作一条特殊"会议"放在列表顶端，让
 * 用户能：
 *   a) 看到"实时记录"也是受关注的内容（有图标 + 正在转写提示）
 *   b) 切回它时中间面板显示全局 ambient feed（TranscriptStream 早就支持）
 *   c) 右侧 minutes / outputs 显示空态（待机不产生会议级产物）
 *
 * 用 currentMeetingId === null 表达"选中实时记录"，避免新增 sentinel 字符串
 * 污染 store 类型。
 */
export default function MeetingList({
  captureState = "capturing",
  onSelect,
}: {
  captureState?: CaptureState;
  onSelect?: () => void;
}): JSX.Element {
  const [query, setQuery] = useState("");
  const meetings = useStore((s) => s.meetings);
  const meetingDetailLoaded = useStore((s) => s.meetingDetailLoaded);
  const meetingDetailErrors = useStore((s) => s.meetingDetailErrors);
  const retryMeetingDetail = useStore((s) => s.retryMeetingDetail);
  const meetingListLoadPhase = useStore((s) => s.meetingListLoadPhase);
  const meetingListError = useStore((s) => s.meetingListError);
  const meetingListLastSuccessAt = useStore((s) => s.meetingListLastSuccessAt);
  const retryMeetingListLoad = useStore((s) => s.retryMeetingListLoad);
  const currentId = useStore((s) => s.currentMeetingId);
  const select = useStore((s) => s.selectMeeting);

  // 过滤掉 < 10s 的鱼蚂会议（用户测试 manual_start → 立刻 manual_end 残留）
  // 规则：state=ended（不是 finalized；finalized 留着）+ 持续 < 10s + 无 minutes
  // 后端 cleanup script 已把已存在的脏数据清了；这里防御未来再产生
  const SHORT_THRESHOLD_MS = 10_000;
  const allItems = Object.values(meetings)
    .filter((m) => {
      if (m.state !== "ended") return true; // in_meeting / finalized 都留
      if (m.minutes || m.minutes_status === "ok") return true; // 有纪要的留
      if (!m.started_at || !m.ended_at) return true; // 时间字段不全的留（保守）
      const dur = Date.parse(m.ended_at) - Date.parse(m.started_at);
      return dur >= SHORT_THRESHOLD_MS;
    })
    .sort((a, b) =>
      (b.started_at ?? "").localeCompare(a.started_at ?? ""),
    );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const items = normalizedQuery
    ? allItems.filter((meeting) =>
        meetingDisplayTitle(meeting)
          .toLocaleLowerCase()
          .includes(normalizedQuery),
      )
    : allItems;

  const ambientActive = currentId === null;
  const ambientButton = (
    <button
      key="__ambient__"
      data-testid="meeting-item-ambient"
      onClick={() => {
        select(null);
        onSelect?.();
      }}
      className={`w-full overflow-hidden text-left px-2.5 py-2 rounded-md transition-colors ${
        ambientActive
          ? "bg-paper-300/70 text-ink-900"
          : "hover:bg-paper-200 text-ink-700"
      }`}
    >
      <div className="flex min-w-0 items-center gap-2">
        <Radio className="w-3 h-3 shrink-0 text-accent" aria-hidden="true" />
        <span className="block min-w-0 flex-1 truncate text-[13px] font-medium">
          实时记录
        </span>
      </div>
      <div className="mt-1 min-w-0 overflow-hidden whitespace-nowrap pl-[18px] text-[11px] text-ink-400">
        {captureState === "capturing"
          ? "正在转写"
          : captureState === "initializing"
            ? "正在准备麦克风"
            : "麦克风不可用"}
      </div>
    </button>
  );

  const searchBox = (
    <label className="mb-2 flex h-8 shrink-0 items-center gap-2 rounded-md border border-paper-300 bg-white px-2.5 text-ink-500 focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/10">
      <Search className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索会议"
        aria-label="搜索会议"
        data-testid="meeting-search-input"
        className="min-w-0 flex-1 bg-transparent text-[12px] text-ink-800 outline-none placeholder:text-ink-400"
      />
    </label>
  );

  if (allItems.length === 0) {
    return (
      <>
        {searchBox}
        <div
          className="echodesk-meeting-list-scroll min-h-0 flex-1 space-y-0.5 overflow-y-auto pr-0.5"
          data-testid="meeting-list-scroll"
        >
          {ambientButton}
          {meetingListLoadPhase === "loading" || meetingListLoadPhase === "idle" ? (
            <div
              className="mx-2 mt-4 flex items-center gap-2 rounded-md border border-paper-300 bg-paper-100 px-3 py-3 text-[11px] text-ink-500"
              data-testid="meeting-list-loading"
              role="status"
            >
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              正在加载历史会议…
            </div>
          ) : meetingListLoadPhase === "error" ? (
            <div
              className="mx-2 mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-3 text-[11px] text-red-700"
              data-testid="meeting-list-error"
              role="alert"
            >
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span>{meetingListError ?? "历史会议暂时无法加载"}</span>
              </div>
              <button
                type="button"
                className="mt-2 inline-flex items-center gap-1 rounded border border-red-200 bg-white px-2 py-1 font-medium hover:bg-red-100"
                onClick={retryMeetingListLoad}
                data-testid="retry-meeting-list"
              >
                <RefreshCw className="h-3 w-3" aria-hidden="true" />
                重试加载
              </button>
            </div>
          ) : (
            <div className="px-2 pt-4 pb-2" data-testid="meeting-list-empty">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span className="text-ink-400 text-[11px]">
                    暂无会议
                    <br />
                    开始会议后会保存在这里
                  </span>
                }
              />
            </div>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      {searchBox}
      <div
        className="echodesk-meeting-list-scroll min-h-0 flex-1 space-y-0.5 overflow-y-auto pr-0.5"
        data-testid="meeting-list-scroll"
      >
        {ambientButton}
        {meetingListLoadPhase === "loading" && (
          <div
            className="mx-1 my-2 flex items-center gap-1.5 rounded-md border border-paper-300 bg-paper-100 px-2.5 py-2 text-[10px] text-ink-500"
            data-testid="meeting-list-loading-cached"
            role="status"
          >
            <LoaderCircle className="h-3 w-3 animate-spin" aria-hidden="true" />
            正在同步历史会议，当前内容会继续保留
          </div>
        )}
        {meetingListLoadPhase === "degraded" && (
          <div
            className="mx-1 my-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-2 text-[10px] leading-relaxed text-amber-700"
            data-testid="meeting-list-degraded"
            role="status"
          >
            <div className="flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              <span title={meetingListLastSuccessAt ? `上次同步：${meetingListLastSuccessAt}` : undefined}>
                历史同步失败，正在显示上次内容
                {meetingListLastSuccessAt ? "。" : ""}
              </span>
            </div>
            <button
              type="button"
              className="mt-1 inline-flex items-center gap-1 font-medium underline underline-offset-2"
              onClick={retryMeetingListLoad}
              data-testid="retry-meeting-list"
            >
              <RefreshCw className="h-3 w-3" aria-hidden="true" />
              重新同步
            </button>
          </div>
        )}
        <div className="h-px bg-paper-300 my-1" aria-hidden="true" />
        {items.length === 0 && (
          <div className="px-3 py-8 text-center text-[11px] text-ink-400">
            未找到匹配会议
          </div>
        )}
        {items.map((m) => {
          const active = currentId === m.meeting_id;
          const detailLoaded = meetingDetailLoaded[m.meeting_id] === true;
          const detailError = meetingDetailErrors[m.meeting_id];
          const segmentCount = detailLoaded
            ? m.segments.length
            : Math.max(m.summary_segment_count ?? 0, m.segments.length);
          const segmentSpeakerCount = countDisplaySpeakers(m.segments);
          const speakerCount = detailLoaded
            ? segmentSpeakerCount
            : Math.max(m.summary_speaker_count ?? 0, segmentSpeakerCount);
          // M_minutes_refactor：左侧列表显示语义化标题（如「直播带货话术 + AI 编程营销
          // 讨论」），优先语义标题；没有标题时用开始时间区分，不暴露内部 ID。
          const friendlyTitle = meetingDisplayTitle(m);
          return (
            <button
              key={m.meeting_id}
              data-testid="meeting-item"
              data-meeting-id={m.meeting_id}
              aria-current={active ? "page" : undefined}
              onClick={() => {
                if (active && detailError) retryMeetingDetail(m.meeting_id);
                select(m.meeting_id);
                onSelect?.();
              }}
              className={`w-full overflow-hidden text-left px-2.5 py-2 rounded-md transition-colors ${
                active
                  ? "bg-paper-300/70 text-ink-900"
                  : "hover:bg-paper-200 text-ink-700"
              }`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <span
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot[m.state]}`}
                />
                <span
                  className="block min-w-0 flex-1 truncate text-[13px] font-medium"
                  title={friendlyTitle}
                  data-testid="meeting-item-title"
                >
                  {friendlyTitle}
                </span>
              </div>
              <div className="mt-1 flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap pl-3.5 text-[11px] text-ink-400">
                <span className="shrink-0">{label[m.state]}</span>
                <span className="shrink-0">·</span>
                <span className="shrink-0">{segmentCount} 段</span>
                <span className="shrink-0">·</span>
                {/* 与 TranscriptStream 同源：基于 remap 后的 displayIdx
                    数 distinct，避免"显示到说话人 47 但列表写 86 人" */}
                <span className="shrink-0">{speakerCount} 人</span>
              </div>
              {active && detailError && (
                <div className="mt-1 pl-3.5 text-[10px] text-err">
                  加载失败 · 点击重试
                </div>
              )}
            </button>
          );
        })}
      </div>
    </>
  );
}
