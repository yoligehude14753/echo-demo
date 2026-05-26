import { useEffect, useRef } from "react";
import { Empty } from "antd";
import { useStore } from "@/store";

const speakerColors = [
  { fg: "#10a37f", bg: "#ecfdf5" },
  { fg: "#2563eb", bg: "#eff6ff" },
  { fg: "#d97706", bg: "#fffbeb" },
  { fg: "#db2777", bg: "#fdf2f8" },
  { fg: "#7c3aed", bg: "#f5f3ff" },
];

function colorForSpeaker(
  label: string | null | undefined,
): { fg: string; bg: string } {
  if (!label) return { fg: "#737373", bg: "#f5f5f5" };
  const idx = parseInt(label.replace(/[^\d]/g, ""), 10) || 0;
  return speakerColors[idx % speakerColors.length];
}

function fmtMs(ms: number): string {
  const s = Math.floor(ms / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

export default function TranscriptStream(): JSX.Element {
  const currentId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentId ? s.meetings[currentId] : undefined,
  );
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [meeting?.segments.length]);

  if (!meeting) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span className="text-ink-400 text-[11px]">
              从左侧选择会议查看转写流
            </span>
          }
        />
      </div>
    );
  }

  if (meeting.segments.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-ink-400 text-[12px]">
        等待转写片段…
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-8 py-6">
      <div className="max-w-3xl mx-auto space-y-4">
        {meeting.segments.map((s, idx) => {
          const c = colorForSpeaker(s.speaker_label);
          return (
            <div
              key={`${s.start_ms}-${idx}`}
              className="flex gap-3 items-start"
            >
              <span className="text-[10px] text-ink-400 font-mono shrink-0 pt-1 w-10 text-right">
                {fmtMs(s.start_ms)}
              </span>
              <span
                className="text-[11px] font-medium shrink-0 px-2 py-0.5 rounded-full"
                style={{ color: c.fg, background: c.bg }}
              >
                {s.speaker_label ?? "未识别"}
              </span>
              <span className="text-[14px] text-ink-800 leading-7 flex-1">
                {s.text}
              </span>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}
