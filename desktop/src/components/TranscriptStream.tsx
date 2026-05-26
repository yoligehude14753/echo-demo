import { useEffect, useRef } from "react";
import { Empty } from "antd";
import { useStore } from "@/store";

const speakerPalette = ["#5b8cff", "#3ecf8e", "#f0b429", "#ff6b6b", "#a78bfa"];

function colorForSpeaker(label: string | null | undefined): string {
  if (!label) return "#94a3b8";
  const idx = parseInt(label.replace(/[^\d]/g, ""), 10) || 0;
  return speakerPalette[idx % speakerPalette.length];
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
      <div className="h-full flex items-center justify-center">
        <Empty
          description={
            <span className="text-slate-500 text-xs">
              选择一个会议查看转写流
            </span>
          }
        />
      </div>
    );
  }

  if (meeting.segments.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-slate-500 text-sm">
        等待转写片段…
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-120px)] overflow-y-auto px-6 py-4 space-y-3">
      {meeting.segments.map((s, idx) => {
        const c = colorForSpeaker(s.speaker_label);
        return (
          <div key={`${s.start_ms}-${idx}`} className="flex gap-3 items-start">
            <span className="text-xs text-slate-500 font-mono shrink-0 pt-0.5">
              {fmtMs(s.start_ms)}
            </span>
            <span
              className="text-xs font-medium shrink-0 pt-0.5"
              style={{ color: c, minWidth: 64 }}
            >
              {s.speaker_label ?? "未识别"}
            </span>
            <span className="text-sm text-slate-100 leading-relaxed">
              {s.text}
            </span>
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}
