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

// ── RAG 文档（M6：聊天框拖入 + 工作区） ──────────────────────────

export interface RagDocSummary {
  doc_id: string;
  title: string;
  kind: string;
  source: string; // upload / workspace / meeting
  source_path: string | null;
  n_chunks: number;
}

export interface RagDocsResponse {
  total: number;
  by_source: Record<string, RagDocSummary[]>;
  docs: RagDocSummary[];
}

export async function ingestFile(
  file: File,
  title?: string,
): Promise<{ doc_id: string; title: string }> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  if (title) fd.append("title", title);
  const u = await apiUrl("/rag/ingest");
  const r = await fetch(u, { method: "POST", body: fd });
  return asJson(r);
}

export async function listRagDocs(): Promise<RagDocsResponse> {
  const u = await apiUrl("/rag/docs");
  const r = await fetch(u);
  return asJson<RagDocsResponse>(r);
}

export async function deleteRagDoc(docId: string): Promise<void> {
  const u = await apiUrl(`/rag/docs/${encodeURIComponent(docId)}`);
  const r = await fetch(u, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete ${r.status}`);
}

// ── 授权工作区 ─────────────────────────────────────────────

export interface WorkspaceStatus {
  configured_dirs: string[];
  authorized_dirs: string[];
  n_indexed: number;
  max_file_mb: number;
  scan_on_startup: boolean;
}

export interface WorkspaceScanResult {
  n_total: number;
  n_added: number;
  n_updated: number;
  n_removed: number;
  n_skipped: number;
  n_failed: number;
  duration_s: number;
  errors: string[];
}

export async function workspaceStatus(): Promise<WorkspaceStatus> {
  const u = await apiUrl("/workspace/status");
  const r = await fetch(u);
  return asJson<WorkspaceStatus>(r);
}

export async function workspaceScan(): Promise<WorkspaceScanResult> {
  const u = await apiUrl("/workspace/scan");
  const r = await fetch(u, { method: "POST" });
  return asJson<WorkspaceScanResult>(r);
}

export async function workspaceClear(): Promise<{ n_removed: number }> {
  const u = await apiUrl("/workspace/clear");
  const r = await fetch(u, { method: "POST" });
  return asJson(r);
}
