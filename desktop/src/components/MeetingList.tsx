import { Empty, List, Tag } from "antd";
import { useStore } from "@/store";
import type { MeetingCard } from "@/types";

const stateColor: Record<MeetingCard["state"], string> = {
  idle: "default",
  in_meeting: "processing",
  ended: "success",
};

const stateText: Record<MeetingCard["state"], string> = {
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
      <Empty
        description={
          <span className="text-slate-500 text-xs">
            尚无会议
            <br />
            等待 backend 推送事件
          </span>
        }
      />
    );
  }

  return (
    <List
      size="small"
      dataSource={items}
      renderItem={(m) => (
        <List.Item
          key={m.meeting_id}
          className={`!px-3 !py-2 cursor-pointer rounded-md ${
            currentId === m.meeting_id ? "bg-bg-700" : "hover:bg-bg-700/60"
          }`}
          onClick={() => select(m.meeting_id)}
        >
          <div className="w-full">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm text-slate-200 truncate">{m.title}</span>
              <Tag color={stateColor[m.state]}>{stateText[m.state]}</Tag>
            </div>
            <div className="text-xs text-slate-500 mt-1 flex items-center justify-between">
              <span>{m.segments.length} 段</span>
              <span>{m.speakers.size} 位说话人</span>
            </div>
          </div>
        </List.Item>
      )}
    />
  );
}
