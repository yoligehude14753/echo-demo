import { Empty, Tag, Typography } from "antd";
import { useStore } from "@/store";

const { Title, Paragraph } = Typography;

export default function MinutesView(): JSX.Element {
  const currentId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentId ? s.meetings[currentId] : undefined,
  );

  if (!meeting?.minutes) {
    return (
      <div className="px-6 py-6 border-b border-bg-700">
        <Empty
          description={
            <span className="text-slate-500 text-xs">
              纪要尚未生成
              <br />
              结束会议后由 MiniMax-M2.7 自动产出
            </span>
          }
        />
      </div>
    );
  }

  const m = meeting.minutes;
  return (
    <div className="px-6 py-4 border-b border-bg-700 max-h-[55vh] overflow-y-auto">
      <Title level={5} className="!text-slate-100 !mb-1">
        {m.title}
      </Title>
      <div className="text-xs text-slate-500 mb-3">
        时长 {Math.round(m.duration_sec)}s · 说话人 {m.speakers.join(" / ")}
      </div>
      <Paragraph className="!text-slate-200 !mb-3 text-sm">
        {m.summary}
      </Paragraph>
      {m.sections.map((sec, i) => (
        <div key={i} className="mb-3">
          <div className="text-sm font-medium text-slate-200 mb-1">
            {sec.heading}
          </div>
          <ul className="list-disc pl-5 text-sm text-slate-300 space-y-0.5">
            {sec.bullets.map((b, j) => (
              <li key={j}>{b}</li>
            ))}
          </ul>
        </div>
      ))}
      {m.decisions.length > 0 && (
        <div className="mb-2">
          <span className="text-xs text-slate-500">决议</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {m.decisions.map((d, i) => (
              <Tag key={i} color="green">
                {d}
              </Tag>
            ))}
          </div>
        </div>
      )}
      {m.action_items.length > 0 && (
        <div>
          <span className="text-xs text-slate-500">行动项</span>
          <ul className="list-disc pl-5 text-sm text-slate-300 mt-1 space-y-0.5">
            {m.action_items.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
