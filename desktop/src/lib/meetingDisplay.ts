import type { MeetingCard } from "@/types";

function formatMeetingStart(iso: string | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${month}月${day}日 ${hours}:${minutes}`;
}

/**
 * 用户界面只展示可读名称，不把数据库 meeting_id 当成标题泄露出来。
 * 没有语义标题时用会议开始时间区分记录。
 */
export function meetingDisplayTitle(
  meeting: MeetingCard | undefined,
  fallback = "未命名会议",
): string {
  if (!meeting) return fallback;
  const displayTitle = meeting.display_title?.trim();
  if (displayTitle) return displayTitle;
  const title = meeting.title?.trim();
  if (title && title !== meeting.meeting_id) return title;
  const startedAt = formatMeetingStart(meeting.started_at);
  return startedAt ? `会议 · ${startedAt}` : fallback;
}
