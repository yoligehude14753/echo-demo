import { Empty } from "antd";
import { useStore } from "@/store";
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

export default function MeetingList(): JSX.Element {
  const meetings = useStore((s) => s.meetings);
  const currentId = useStore((s) => s.currentMeetingId);
  const select = useStore((s) => s.selectMeeting);

  const items = Object.values(meetings).sort((a, b) =>
    (b.started_at ?? "").localeCompare(a.started_at ?? ""),
  );

  if (items.length === 0) {
    return (
      <div className="px-2 py-8">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span className="text-ink-400 text-[11px]">
              暂无会议
              <br />
              等待事件
            </span>
          }
        />
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {items.map((m) => {
        const active = currentId === m.meeting_id;
        return (
          <button
            key={m.meeting_id}
            onClick={() => select(m.meeting_id)}
            className={`w-full text-left px-2.5 py-2 rounded-md transition-colors ${
              active
                ? "bg-paper-300/70 text-ink-900"
                : "hover:bg-paper-200 text-ink-700"
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot[m.state]}`} />
              <span className="text-[13px] font-medium truncate flex-1">
                {m.title}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-ink-400 flex items-center gap-2 pl-3.5">
              <span>{label[m.state]}</span>
              <span>·</span>
              <span>{m.segments.length} 段</span>
              <span>·</span>
              <span>{m.speakers.size} 人</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
