import type {
  GeneratedArtifact,
  IntentResult,
  MeetingMinutes,
  MeetingStateSnapshot,
  TranscriptSegment,
} from "@/types";
import {
  DEFAULT_ANDROID_BACKEND_BASE,
  apiPath,
  apiUrl,
  backendBase,
  configuredBackendBase,
  isNativeMobile,
  shareBackendBase,
} from "@/runtime";

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

/**
 * 7 道门处理结果分流标签（与 backend/app/schemas/capture.py:SttStatus 一一对应）。
 * captureChunkRouter 用 `circuit_open` 触发优雅止血（指数退避停止上传）。
 */
export type SttStatus = "ok" | "empty" | "failed" | "circuit_open" | "gated";

export interface CaptureChunkResponse {
  ambient_stored: boolean;
  ambient_text: string | null;
  audio_ref: string;
  speaker_id?: string | null;
  speaker_label?: string | null;
  meeting_segments: TranscriptSegment[];
  /** M_diag_brake：让前端能区分被哪道门吃了，仅 circuit_open 触发止血。 */
  stt_status: SttStatus;
}

export async function uploadCaptureChunk(
  blob: Blob,
  sampleRate = 16000,
  meetingId?: string,
): Promise<CaptureChunkResponse> {
  const fd = new FormData();
  fd.append("audio", blob, "chunk.wav");
  fd.append("sample_rate", String(sampleRate));
  if (meetingId) fd.append("meeting_id", meetingId);
  const u = await apiUrl("/capture/chunk");
  const r = await fetch(u, { method: "POST", body: fd });
  // backend 在引入 stt_status 字段前的旧版本可能不返回；缺省视为 "ok"。
  const parsed = await asJson<CaptureChunkResponse>(r);
  if (!parsed.stt_status) parsed.stt_status = "ok";
  return parsed;
}

/**
 * AmbientCapturePipeline 7 道门处理结果计数（进程级 in-memory，重启清零）。
 * 与 backend/app/use_cases/ambient_capture.py:AmbientStats 一一对应。
 */
export interface CaptureStats {
  chunks_total: number;
  gated_rms: number;
  gated_low_speech: number;
  stt_circuit_open: number;
  stt_failed: number;
  stt_empty: number;
  hallu_dropped: number;
  diarize_failed: number;
  /**
   * phase4-diar-deep：diarizer 正常跑了但说不出（短段无匹配 / 切不出 voiced）。
   * 与 diarize_failed（diarizer 抛异常）区分，便于排查"57 段未识别"根因分布。
   */
  diarize_returned_none: number;
  stored: number;
  last_chunk_at: string | null;
  last_stored_at: string | null;
  last_rms: number;
  last_speech_ratio: number;
  last_gate_reason: string | null;
}

export async function getCaptureStats(): Promise<CaptureStats> {
  const u = await apiUrl("/capture/stats");
  const r = await fetch(u);
  return asJson<CaptureStats>(r);
}

export async function endMeeting(meetingId: string): Promise<void> {
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/end`);
  const r = await fetch(u, { method: "POST" });
  if (!r.ok) throw new Error(`end ${r.status}`);
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

/**
 * 重试生成纪要（后端 POST /meetings/{id}/finalize 是幂等的）。
 *
 * 用户场景：前次 finalize 失败 → 会议进入 ``minutes_status="generation_failed"``
 * → MinutesView 给「重试」按钮 → 调本 API 重新跑 LLM，覆盖 minutes_json。
 */
export async function retryMinutesGeneration(
  meetingId: string,
  title: string,
): Promise<MeetingMinutes> {
  return finalizeMeeting(meetingId, title);
}

// ── 全局会议状态机（PRD：自动开/结 + 手动覆盖） ──────────────────

export async function getCurrentMeeting(): Promise<MeetingStateSnapshot> {
  const u = await apiUrl("/meetings/current");
  const r = await fetch(u);
  return asJson<MeetingStateSnapshot>(r);
}

export async function manualStartMeeting(
  title?: string,
): Promise<MeetingStateSnapshot> {
  const u = await apiUrl("/meetings/manual_start");
  // 不带 title 时直接发空 POST，避免空 multipart body 被 backend 拒绝
  // （Fastapi Form(None) 仍要求 Content-Type: multipart，且必须有边界内容）
  let init: RequestInit = { method: "POST" };
  if (title && title.trim()) {
    const fd = new FormData();
    fd.append("title", title);
    init = { method: "POST", body: fd };
  }
  const r = await fetch(u, init);
  return asJson<MeetingStateSnapshot>(r);
}

export async function manualEndMeeting(): Promise<MeetingStateSnapshot> {
  const u = await apiUrl("/meetings/manual_end");
  const r = await fetch(u, { method: "POST" });
  return asJson<MeetingStateSnapshot>(r);
}

// ── 会议历史（M_meeting_history）─────────────────────────────
//
// 这 4 个 export 是"读 only"，专门给左侧会议列表 + 切换中右面板用的。
// 不替换 startMeeting / endMeeting / uploadCaptureChunk 等 mutating endpoint。
// 后端见 backend/app/api/meetings.py 的 GET /meetings、/{id}/transcript、
// /{id}/minutes、/{id}/artifacts 四个 endpoint。

export interface MeetingSummary {
  meeting_id: string;
  title: string | null;
  /** M_minutes_refactor：LLM finalize 时生成的语义化标题（替代左侧列表里
   *  显示的 meeting_id）；未生成时为 null。前端按
   *  ``display_title || title || meeting_id`` 顺序兜底展示。 */
  display_title: string | null;
  state: "in_meeting" | "ended" | "finalized";
  started_at: string;
  ended_at: string | null;
  finalized_at: string | null;
  n_segments: number;
  n_speakers: number;
  has_minutes: boolean;
}

export async function listMeetings(limit = 50): Promise<MeetingSummary[]> {
  const u = await apiUrl(`/meetings?limit=${limit}`);
  const r = await fetch(u);
  return asJson<MeetingSummary[]>(r);
}

export async function getMeetingTranscript(
  meetingId: string,
): Promise<TranscriptSegment[]> {
  const u = await apiUrl(
    `/meetings/${encodeURIComponent(meetingId)}/transcript`,
  );
  const r = await fetch(u);
  return asJson<TranscriptSegment[]>(r);
}

/** 拿不到（404 / 未生成）时返回 null，调用方据此显示"暂无纪要"。 */
export async function getMeetingMinutes(
  meetingId: string,
): Promise<MeetingMinutes | null> {
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/minutes`);
  const r = await fetch(u);
  if (r.status === 404) return null;
  return asJson<MeetingMinutes>(r);
}

export async function getMeetingArtifacts(
  meetingId: string,
): Promise<GeneratedArtifact[]> {
  const u = await apiUrl(
    `/meetings/${encodeURIComponent(meetingId)}/artifacts`,
  );
  const r = await fetch(u);
  // 当前后端实现总是返回空数组（详见 meetings.py 注释）；调用约定保留以便
  // 后续接入 DB join 时只换实现，前端不动。会议不存在仍返回 404。
  if (r.status === 404) return [];
  return asJson<GeneratedArtifact[]>(r);
}

export interface ClearMeetingOutputsResult {
  meeting_id: string;
  minutes_cleared: boolean;
  artifact_ids: string[];
  artifacts_deleted: number;
  missing_artifact_ids: string[];
}

export async function meetingShareUrl(
  meetingId: string,
  artifactIds: string[] = [],
): Promise<string> {
  const params = new URLSearchParams();
  const ids = artifactIds.filter(Boolean);
  if (ids.length > 0) params.set("artifact_ids", ids.join(","));
  const path = `/meetings/${encodeURIComponent(meetingId)}/share${
    params.toString() ? `?${params.toString()}` : ""
  }`;
  const base = await shareBackendBase();
  if (base) {
    const useApiPrefix =
      typeof window !== "undefined" &&
      base === window.location.origin &&
      !window.echo?.isElectron;
    return `${base}${useApiPrefix ? apiPath(path) : path}`;
  }
  if (typeof window !== "undefined" && window.location.origin) {
    return `${window.location.origin}${apiPath(path)}`;
  }
  return apiPath(path);
}

export async function clearMeetingOutputs(
  meetingId: string,
  artifactIds: string[],
): Promise<ClearMeetingOutputsResult> {
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/outputs`);
  const r = await fetch(u, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ artifact_ids: artifactIds, clear_minutes: true }),
  });
  return asJson<ClearMeetingOutputsResult>(r);
}

// ── 待机时持续显示 ambient 转写片段 ──────────────────────────

export interface AmbientSegment {
  text: string;
  captured_at: string;
  speaker_id: string | null;
  speaker_label: string | null;
  duration_ms: number;
}

export async function listRecentAmbient(
  limit = 50,
): Promise<AmbientSegment[]> {
  const u = await apiUrl(`/capture/recent?limit=${limit}`);
  const r = await fetch(u);
  return asJson<AmbientSegment[]>(r);
}

export type ArtifactKind =
  | "word"
  | "docx"
  | "xlsx"
  | "excel"
  | "pptx"
  | "ppt"
  | "html"
  | "markdown"
  | "md"
  | "mdown"
  | "pdf"
  | "txt"
  | "text";

export async function generateArtifact(req: {
  artifact_type: ArtifactKind;
  brief: string;
  extra_instructions?: string;
  /** M_minutes_refactor：当指令来自会议待办「执行」按钮时一并传，后端
   *  生成成功后会回写 ``meetings.minutes_json.todos[id].status=done``。 */
  meeting_id?: string;
  todo_id?: string;
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
  const configured = configuredBackendBase();
  if (configured || isNativeMobile()) {
    const host = configured ?? DEFAULT_ANDROID_BACKEND_BASE;
    return `${host}/artifacts/${encodeURIComponent(artifactId)}/download`;
  }
  if (
    typeof window !== "undefined" &&
    window.location.protocol === "file:"
  ) {
    const host = "http://127.0.0.1:8769";
    return `${host}/artifacts/${encodeURIComponent(artifactId)}/download`;
  }
  return `/api/artifacts/${encodeURIComponent(artifactId)}/download`;
}

/**
 * 解析后端 /rag/ask 的 SSE 响应。
 *
 * 协议（见 backend/app/api/retrieval.py _sse）：
 *   - 第 1 帧：`data: {"meta": {...citations...}}`
 *   - 中间帧：`data: {"delta": "..."}` 多次
 *   - 结束帧：`data: [DONE]`
 */
export async function ragAsk(question: string): Promise<{
  answer: string;
  citations: Array<{
    kind: string;
    doc_id?: string;
    title?: string;
    url?: string;
    snippet?: string;
    score?: number;
  }>;
  arbitration: string;
}> {
  const u = await apiUrl("/rag/ask");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  // 兼容：若后端非 SSE（如 mock 测试返 JSON），直接 json() 解析
  const ct = r.headers.get("content-type") ?? "";
  if (!ct.includes("text/event-stream")) {
    return (await r.json()) as Awaited<ReturnType<typeof ragAsk>>;
  }

  const reader = r.body?.getReader();
  if (!reader) throw new Error("rag/ask: response body unreadable");
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let answer = "";
  let citations: Array<Record<string, unknown>> = [];
  let arbitration = "rag";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let nl: number;
    while ((nl = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, nl);
      buf = buf.slice(nl + 2);
      const dataLine = block.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const payload = dataLine.slice("data: ".length);
      if (payload === "[DONE]") continue;
      try {
        const obj = JSON.parse(payload) as Record<string, unknown>;
        if ("delta" in obj && typeof obj.delta === "string") {
          answer += obj.delta;
        } else if ("meta" in obj && obj.meta && typeof obj.meta === "object") {
          const meta = obj.meta as Record<string, unknown>;
          if (Array.isArray(meta.citations)) {
            citations = meta.citations as Array<Record<string, unknown>>;
          }
          if (typeof meta.chosen_source === "string") {
            arbitration = meta.chosen_source;
          }
        }
      } catch {
        // 单帧解析失败不致命，继续
      }
    }
  }

  return {
    answer,
    citations: citations as Array<{
      kind: string;
      doc_id?: string;
      title?: string;
      url?: string;
      snippet?: string;
      score?: number;
    }>,
    arbitration,
  };
}

/**
 * P4-fix-rag-chat（2026-05-28）：纯 LLM 闲聊（不查 RAG）。
 *
 * 用户痛点：默认 chat 路径已改成走 RAG（解决"基于附件回答"被忽略 PDF 的痛点）；
 * 但偶尔用户只是想跟 LLM 寒暄（"@chat 你好"），不需要 RAG 检索消耗时间。
 * 这条函数对接 backend POST /chat（SSE）→ 把流累积成完整字符串。
 *
 * 与 ragAsk 的区别：
 *  - ragAsk: POST /rag/ask，先检索 RAG/Web 再生成，返回 {answer, citations, ...}
 *  - chatAsk: POST /chat，直接走 LLM，返回纯字符串答案
 */
export async function chatAsk(question: string): Promise<string> {
  const u = await apiUrl("/chat");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  const ct = r.headers.get("content-type") ?? "";
  if (!ct.includes("text/event-stream")) {
    const obj = (await r.json()) as { delta?: string; content?: string };
    return obj.delta ?? obj.content ?? "";
  }

  const reader = r.body?.getReader();
  if (!reader) throw new Error("chat: response body unreadable");
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let answer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, nl);
      buf = buf.slice(nl + 2);
      const dataLine = block.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const payload = dataLine.slice("data: ".length);
      if (payload === "[DONE]") continue;
      try {
        const obj = JSON.parse(payload) as { delta?: string; error?: string };
        if (typeof obj.delta === "string") {
          answer += obj.delta;
        } else if (typeof obj.error === "string") {
          throw new Error(obj.error);
        }
      } catch {
        /* 单帧解析失败不致命 */
      }
    }
  }

  return answer;
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

/**
 * P4-fix-rag-chat（2026-05-28）：让 SettingsPanel 一键把"~/Documents"等大目录
 * 加进 workspace_dirs，并立即触发扫描，把整个文件夹的可索引文件批量入库。
 *
 * 痛点：旧 UX 让用户改 ~/.echodesk/config.json 里 ``workspace_dirs="xxx,yyy"``，
 * 用户既不知道路径在哪也不知道字段名。新做法：GUI dialog 选目录 → 一键完成。
 *
 * 后端实现：把 path 追加到 user.json 的 workspace_dirs CSV，原地更新 settings，
 * 然后 fire-and-forget 扫描。返回前先把新加的 dir 报回来用作 UI 反馈。
 */
export async function workspaceAddDir(
  path: string,
): Promise<{ added: boolean; path: string; configured_dirs: string[] }> {
  const u = await apiUrl("/workspace/add-dir");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return asJson(r);
}

/**
 * 配合 add-dir：让用户能"减一个目录"，等同 add-dir 的反向操作。
 * 不会清空已索引的 doc（保留 RAG 数据），只把该目录从配置里摘掉，
 * 下次扫描时该目录下的文件会被识别为"消失"并 RAG.delete。
 */
export async function workspaceRemoveDir(
  path: string,
): Promise<{ removed: boolean; path: string; configured_dirs: string[] }> {
  const u = await apiUrl("/workspace/remove-dir");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return asJson(r);
}

// ── TTS ─────────────────────────────────────────────────────

/**
 * 后端 /tts/speak 失败时抛出的结构化错误。
 *
 * 与裸 Error 的区别：携带 backend 文本（如 ``tts_silent_output:..``），
 * 让上层（useTtsPlayer / message.error）能给用户一句话人话。
 */
export class TtsSpeakError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail: string,
  ) {
    super(message);
    this.name = "TtsSpeakError";
  }
}

function summarizeTtsDetail(status: number, raw: string): string {
  // 后端返回 FastAPI HTTPException → ``{"detail": "tts_silent_output: ..."}``
  // 但其它非 HTTPException 路径可能是裸字符串；两种都要兼容。
  let detail = raw;
  try {
    const obj = JSON.parse(raw) as { detail?: unknown };
    if (typeof obj.detail === "string") detail = obj.detail;
  } catch {
    /* not json */
  }
  if (detail.startsWith("tts_silent_output")) {
    return "TTS 上游返回静音（可能 qwen3-tts 冷启动），请稍后重试";
  }
  if (detail.startsWith("tts_upstream_error")) {
    return `TTS 上游异常：${detail.replace("tts_upstream_error:", "").trim()}`;
  }
  if (status === 503) return "TTS 已在设置中关闭";
  if (status === 400) return "TTS 文本为空";
  return `TTS 失败：${detail || `HTTP ${status}`}`;
}

/**
 * 拉取 PCM bytes（16kHz 16-bit mono）。前端用 AudioContext 解码播放。
 *
 * 失败时抛 ``TtsSpeakError``（含人类可读 message）；调用方应 message.error
 * 给用户而不是 console.warn 后吞掉。
 */
export async function ttsSpeak(
  text: string,
  voice?: string,
): Promise<ArrayBuffer> {
  const u = await apiUrl("/tts/speak");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice }),
  });
  if (!r.ok) {
    const raw = await r.text();
    throw new TtsSpeakError(summarizeTtsDetail(r.status, raw), r.status, raw);
  }
  return await r.arrayBuffer();
}

/**
 * TTS 子系统真实健康（与单纯 TCP probe 区分）：跑一次合成回环判断。
 * 与 backend /tts/diag 一一对应。
 */
export interface TtsDiagResult {
  ok: boolean;
  state: "ok" | "disabled" | "upstream_error" | "silent_output" | "empty";
  detail: string | null;
  latency_ms: number | null;
  pcm_bytes: number | null;
  rms: number | null;
  peak: number | null;
  voice: string | null;
  base_url: string | null;
  checked_at: number;
}

export async function ttsDiag(opts: { fresh?: boolean } = {}): Promise<TtsDiagResult> {
  const path = opts.fresh ? "/tts/diag?fresh=true" : "/tts/diag";
  const u = await apiUrl(path);
  const r = await fetch(u, { method: "GET" });
  return asJson<TtsDiagResult>(r);
}

export async function listSpeakers(): Promise<
  Array<{
    speaker_id: string;
    label: string | null;
    n_samples: number;
    first_seen_at: string;
    last_seen_at: string;
  }>
> {
  const u = await apiUrl("/speakers");
  const r = await fetch(u);
  return asJson(r);
}

export async function renameSpeaker(
  speakerId: string,
  label: string,
): Promise<void> {
  const u = await apiUrl(`/speakers/${encodeURIComponent(speakerId)}/rename`);
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
  if (!r.ok) throw new Error(`rename ${r.status}`);
}
