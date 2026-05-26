import type {
  GeneratedArtifact,
  IntentResult,
  MeetingMinutes,
  TranscriptSegment,
} from "@/types";
import { apiUrl, backendBase } from "@/runtime";

async function asJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return (await resp.json()) as T;
}

export async function startMeeting(meetingId: string): Promise<void> {
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/start`);
  const r = await fetch(u, { method: "POST" });
  if (!r.ok && r.status !== 204) throw new Error(`start ${r.status}`);
}

export async function uploadChunk(
  meetingId: string,
  blob: Blob,
  sampleRate = 16000,
): Promise<TranscriptSegment[]> {
  const fd = new FormData();
  fd.append("audio", blob, "chunk.wav");
  fd.append("sample_rate", String(sampleRate));
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/chunk`);
  const r = await fetch(u, { method: "POST", body: fd });
  return asJson<TranscriptSegment[]>(r);
}

export async function finalizeMeeting(
  meetingId: string,
  title: string,
): Promise<MeetingMinutes> {
  const fd = new FormData();
  fd.append("title", title);
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/finalize`);
  const r = await fetch(u, { method: "POST", body: fd });
  return asJson<MeetingMinutes>(r);
}

export type ArtifactKind = "word" | "xlsx" | "excel" | "pptx" | "ppt" | "html";

export async function generateArtifact(req: {
  artifact_type: ArtifactKind;
  brief: string;
  extra_instructions?: string;
}): Promise<GeneratedArtifact> {
  const u = await apiUrl("/artifacts/generate");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  return asJson<GeneratedArtifact>(r);
}

export function artifactDownloadUrl(artifactId: string): string {
  // 同步版本：浏览器 / vite dev 用相对路径；Electron file:// 下用 sync sentinel
  // 由于 backendBase() 是异步的，但下载/预览只在前端运行时拼接，简单起见在 file:// 下回退默认 host。
  if (
    typeof window !== "undefined" &&
    window.location.protocol === "file:"
  ) {
    const host = "http://127.0.0.1:8769";
    return `${host}/artifacts/${encodeURIComponent(artifactId)}/download`;
  }
  return `/api/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export async function ragAsk(question: string): Promise<{
  answer: string;
  citations: Array<{ doc_id: string; title: string; snippet: string }>;
  arbitration: string;
}> {
  const u = await apiUrl("/rag/ask");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  return asJson(r);
}

// 用于预热缓存（main.tsx 调）
export async function bootstrapBase(): Promise<void> {
  await backendBase();
}

export async function routeIntent(
  text: string,
  currentMeetingId: string | null,
): Promise<IntentResult> {
  const u = await apiUrl("/intent/route");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      current_meeting_id: currentMeetingId ?? undefined,
    }),
  });
  return asJson<IntentResult>(r);
}
