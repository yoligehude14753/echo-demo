import { Empty } from "antd";
import { FileText } from "lucide-react";
import { useStore } from "@/store";

export default function MinutesView(): JSX.Element {
  const currentId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentId ? s.meetings[currentId] : undefined,
  );

  if (!meeting?.minutes) {
    return (
      <div className="px-6 py-6 border-b border-paper-300">
        <div className="flex items-center gap-2 mb-4 text-[13px] text-ink-700 font-medium">
          <FileText className="w-3.5 h-3.5 text-ink-500" />
          <span>会议纪要</span>
        </div>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span className="text-ink-400 text-[11px]">
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
    <div className="px-6 py-5 border-b border-paper-300 max-h-[55vh] overflow-y-auto">
      <div className="flex items-center gap-2 mb-3 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
      </div>
      <h2 className="brand text-[17px] font-semibold text-ink-900 leading-snug mb-1">
        {m.title}
      </h2>
      <div className="text-[11px] text-ink-400 mb-4 flex items-center gap-1.5">
        <span>时长 {Math.round(m.duration_sec)}s</span>
        <span>·</span>
        <span>{m.speakers.join(" / ")}</span>
      </div>

      <p className="text-[13.5px] text-ink-800 leading-7 mb-5">{m.summary}</p>

      {m.sections.map((sec, i) => (
        <section key={i} className="mb-4">
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            {sec.heading}
          </h3>
          <ul className="space-y-1 text-[13px] text-ink-700">
            {sec.bullets.map((b, j) => (
              <li key={j} className="flex gap-2 leading-6">
                <span className="text-ink-400 shrink-0">·</span>
                <span>{b}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}

      {m.decisions.length > 0 && (
        <section className="mb-4">
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            决议
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {m.decisions.map((d, i) => (
              <span
                key={i}
                className="text-[12px] px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-200"
              >
                {d}
              </span>
            ))}
          </div>
        </section>
      )}

      {m.action_items.length > 0 && (
        <section>
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            行动项
          </h3>
          <ul className="space-y-1 text-[13px] text-ink-700">
            {m.action_items.map((a, i) => (
              <li
                key={i}
                className="flex gap-2 leading-6 pl-2 border-l-2 border-paper-300"
              >
                <span>{a}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
