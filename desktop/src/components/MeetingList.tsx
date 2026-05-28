import { Empty } from "antd";
import { Radio } from "lucide-react";
import { useStore } from "@/store";
import { countDisplaySpeakers } from "@/lib/speakerDisplay";
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
 * 列表第一项是虚拟"伴随时段（自由对话）"——非会议状态的 ambient 转写聚合。
 *
 * 产品决策（2026-05-28）：ambient_segments 表没有 meeting_id 列，所有非会议
 * 时段的语音段共用一个全局 lane。把它当作一条特殊"会议"放在列表顶端，让
 * 用户能：
 *   a) 看到"伴随时段"也是受关注的内容（有图标 + 持续转写中提示）
 *   b) 切回它时中间面板显示全局 ambient feed（TranscriptStream 早就支持）
 *   c) 右侧 minutes / outputs 显示空态（待机不产生会议级产物）
 *
 * 用 currentMeetingId === null 表达"选中伴随时段"，避免新增 sentinel 字符串
 * 污染 store 类型。
 */
export default function MeetingList(): JSX.Element {
  const meetings = useStore((s) => s.meetings);
  const currentId = useStore((s) => s.currentMeetingId);
  const select = useStore((s) => s.selectMeeting);

  // 过滤掉 < 10s 的鱼蚂会议（用户测试 manual_start → 立刻 manual_end 残留）
  // 规则：state=ended（不是 finalized；finalized 留着）+ 持续 < 10s + 无 minutes
  // 后端 cleanup script 已把已存在的脏数据清了；这里防御未来再产生
  const SHORT_THRESHOLD_MS = 10_000;
  const items = Object.values(meetings)
    .filter((m) => {
      if (m.state !== "ended") return true; // in_meeting / finalized 都留
      if (m.minutes) return true; // 有纪要的留
      if (!m.started_at || !m.ended_at) return true; // 时间字段不全的留（保守）
      const dur = Date.parse(m.ended_at) - Date.parse(m.started_at);
      return dur >= SHORT_THRESHOLD_MS;
    })
    .sort((a, b) =>
      (b.started_at ?? "").localeCompare(a.started_at ?? ""),
    );

  const ambientActive = currentId === null;
  const ambientButton = (
    <button
      key="__ambient__"
      data-testid="meeting-item-ambient"
      onClick={() => select(null)}
      className={`w-full text-left px-2.5 py-2 rounded-md transition-colors ${
        ambientActive
          ? "bg-paper-300/70 text-ink-900"
          : "hover:bg-paper-200 text-ink-700"
      }`}
    >
      <div className="flex items-center gap-2">
        <Radio className="w-3 h-3 shrink-0 text-accent" aria-hidden="true" />
        <span className="text-[13px] font-medium truncate flex-1">
          伴随时段
        </span>
      </div>
      <div className="mt-1 text-[11px] text-ink-400 flex items-center gap-2 pl-[18px]">
        <span>自由对话</span>
        <span>·</span>
        <span>持续转写</span>
      </div>
    </button>
  );

  if (items.length === 0) {
    return (
      <div className="space-y-0.5">
        {ambientButton}
        <div className="px-2 pt-4 pb-2">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <span className="text-ink-400 text-[11px]">
                暂无会议
                <br />
                @开始会议 触发
              </span>
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {ambientButton}
      <div className="h-px bg-paper-300 my-1" aria-hidden="true" />
      {items.map((m) => {
        const active = currentId === m.meeting_id;
        // M_minutes_refactor：左侧列表显示语义化标题（如「直播带货话术 + AI 编程营销
        // 讨论」），优先级 display_title > title > meeting_id；保证旧会议、未 finalize
        // 会议都有兜底文案。
        const friendlyTitle =
          (m.display_title && m.display_title.trim()) ||
          (m.title && m.title !== m.meeting_id ? m.title : null) ||
          m.meeting_id;
        return (
          <button
            key={m.meeting_id}
            data-testid="meeting-item"
            data-meeting-id={m.meeting_id}
            onClick={() => select(m.meeting_id)}
            className={`w-full text-left px-2.5 py-2 rounded-md transition-colors ${
              active
                ? "bg-paper-300/70 text-ink-900"
                : "hover:bg-paper-200 text-ink-700"
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot[m.state]}`} />
              <span
                className="text-[13px] font-medium truncate flex-1"
                title={friendlyTitle}
                data-testid="meeting-item-title"
              >
                {friendlyTitle}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-ink-400 flex items-center gap-2 pl-3.5">
              <span>{label[m.state]}</span>
              <span>·</span>
              <span>{m.segments.length} 段</span>
              <span>·</span>
              {/* 与 TranscriptStream 同源：基于 remap 后的 displayIdx
                  数 distinct，避免"显示到说话人 47 但列表写 86 人" */}
              <span>{countDisplaySpeakers(m.segments)} 人</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
