import type { GeneratedArtifact, MeetingMinutes, TranscriptSegment } from "@/types";

const API = "/api";

async function asJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return (await resp.json()) as T;
}

export async function startMeeting(meetingId: string): Promise<void> {
  const r = await fetch(`${API}/meetings/${encodeURIComponent(meetingId)}/start`, {
    method: "POST",
  });
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
  const r = await fetch(`${API}/meetings/${encodeURIComponent(meetingId)}/chunk`, {
    method: "POST",
    body: fd,
  });
  return asJson<TranscriptSegment[]>(r);
}

export async function finalizeMeeting(
  meetingId: string,
  title: string,
): Promise<MeetingMinutes> {
  const fd = new FormData();
  fd.append("title", title);
  const r = await fetch(
    `${API}/meetings/${encodeURIComponent(meetingId)}/finalize`,
    { method: "POST", body: fd },
  );
  return asJson<MeetingMinutes>(r);
}

export async function generateArtifact(req: {
  artifact_type: "word" | "xlsx" | "html";
  brief: string;
  extra_instructions?: string;
}): Promise<GeneratedArtifact> {
  const r = await fetch(`${API}/artifacts/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  return asJson<GeneratedArtifact>(r);
}

export function artifactDownloadUrl(artifactId: string): string {
  return `${API}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export async function ragAsk(question: string): Promise<{
  answer: string;
  citations: Array<{ doc_id: string; title: string; snippet: string }>;
  arbitration: string;
}> {
  const r = await fetch(`${API}/rag/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  return asJson(r);
}
