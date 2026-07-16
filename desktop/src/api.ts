import type {
  GeneratedArtifact,
  AgentTaskCard,
  AgentTaskEvent,
  IntentResult,
  MeetingMinutes,
  MemoryFramePayload,
  MemorySourceCard,
  MeetingStateSnapshot,
  TranscriptSegment,
  WorkflowRunDTO,
} from "@/types";
import {
  BACKEND_ORIGIN_EVENT,
  BackendBasePolicyError,
  type ElectronWorkspaceContext,
  apiPath,
  apiUrl,
  backendBase,
  backendBaseSnapshot,
  backendRole,
  canUseRelativeBackendProxy,
  configuredBackendBase,
  isDefaultPublicBackend,
  isNativeMobile,
  isPublicRuntime,
  runtimeMode,
  shareBackendBase,
} from "@/runtime";
import { apiTransport } from "@/session";
import {
  enqueueSyncOperation,
  ensureSyncDeviceId,
  makeOperationId,
} from "@/syncState";
import {
  normalizeCaptureControl,
  normalizeCaptureDevices,
  type CaptureControl,
  type CaptureControlSnapshot,
  type CaptureMode,
} from "@/capture/captureControl";

const DEFAULT_PROBE_TIMEOUT_MS = 6_000;
const PUBLIC_PROBE_TIMEOUT_MS = 12_000;
const CAPTURE_UPLOAD_TIMEOUT_MS = 20_000;
const TTS_SPEAK_TIMEOUT_MS = 30_000;
// Keep the client envelope slightly wider than each durable backend workflow.
const RAG_QUERY_TIMEOUT_MS = 190_000;
const MEETING_FINALIZE_TIMEOUT_MS = 310_000;
const ARTIFACT_GENERATE_TIMEOUT_MS = 610_000;
const WORKSPACE_SCAN_TIMEOUT_MS = 610_000;
const SSE_MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const TTS_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;

export interface ApiReadOptions {
  signal?: AbortSignal;
}

function fetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  return apiTransport(input, init, { throwHttpErrors: false });
}

async function asJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    await resp.body?.cancel().catch(() => undefined);
    throw new Error(`EchoDesk 服务请求失败（HTTP ${resp.status}）`);
  }
  try {
    return (await resp.json()) as T;
  } catch {
    // JSON.parse errors can echo a fragment of an invalid backend body.
    throw new Error("EchoDesk 服务响应格式无效");
  }
}

function probeTimeoutMs(): number {
  const configured = configuredBackendBase();
  if (
    isNativeMobile() ||
    (configured !== null && isDefaultPublicBackend(configured)) ||
    (typeof window !== "undefined" && window.echo?.isPublicDemo === true)
  ) {
    return PUBLIC_PROBE_TIMEOUT_MS;
  }
  return DEFAULT_PROBE_TIMEOUT_MS;
}

async function fetchProbe(url: string, init: RequestInit = {}): Promise<Response> {
  return apiTransport(
    url,
    { cache: "no-store", ...init },
    { timeoutMs: probeTimeoutMs(), throwHttpErrors: false },
  );
}

async function fetchWithAbortTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  externalSignal?: AbortSignal,
  maxResponseBytes?: number,
): Promise<Response> {
  return apiTransport(
    url,
    { ...init, signal: externalSignal ?? init.signal },
    { timeoutMs, maxResponseBytes, throwHttpErrors: false },
  );
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
export type SttStatus =
  | "ok"
  | "empty"
  | "failed"
  | "circuit_open"
  | "gated"
  | "unknown";

export interface CaptureChunkResponse {
  ambient_stored: boolean;
  ambient_text: string | null;
  audio_ref: string | null;
  speaker_id?: string | null;
  speaker_label?: string | null;
  meeting_id?: string | null;
  meeting_segments: TranscriptSegment[];
  /** M_diag_brake：让前端能区分被哪道门吃了，仅 circuit_open 触发止血。 */
  stt_status: SttStatus;
}

const CAPTURE_STT_STATUSES: readonly SttStatus[] = [
  "ok",
  "empty",
  "failed",
  "circuit_open",
  "gated",
  "unknown",
];

function captureRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function finiteNonNegative(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

function nullableFiniteNonNegative(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return finiteNonNegative(value);
}

function normalizeSttStatus(value: unknown): SttStatus {
  return CAPTURE_STT_STATUSES.includes(value as SttStatus)
    ? (value as SttStatus)
    : "unknown";
}

export function normalizeCaptureChunkResponse(
  value: unknown,
): CaptureChunkResponse {
  const body = captureRecord(value);
  return {
    ambient_stored: body.ambient_stored === true,
    ambient_text: nullableString(body.ambient_text),
    audio_ref: nullableString(body.audio_ref),
    speaker_id: nullableString(body.speaker_id),
    speaker_label: nullableString(body.speaker_label),
    meeting_id: nullableString(body.meeting_id),
    // Older backends did not send this business array. Missing means no
    // overlay segments, never a transport failure after a successful HTTP ack.
    meeting_segments: Array.isArray(body.meeting_segments)
      ? (body.meeting_segments as TranscriptSegment[])
      : [],
    stt_status: normalizeSttStatus(body.stt_status),
  };
}

export async function uploadCaptureChunk(
  blob: Blob,
  sampleRate = 16000,
  meetingId?: string,
  options: {
    signal?: AbortSignal;
    timeoutMs?: number;
    idempotencyKey?: string;
    deviceId?: string;
    segmentId?: string;
  } = {},
): Promise<CaptureChunkResponse> {
  const deviceId = options.deviceId ?? ensureSyncDeviceId();
  const segmentId =
    options.segmentId ??
    `${deviceId}:${globalThis.crypto?.randomUUID?.() ?? Date.now().toString(36)}`;
  const fd = new FormData();
  fd.append("audio", blob, "chunk.wav");
  fd.append("sample_rate", String(sampleRate));
  fd.append("device_id", deviceId);
  fd.append("segment_id", segmentId);
  if (meetingId) fd.append("meeting_id", meetingId);
  const u = await apiUrl("/capture/chunk");
  const r = await fetchWithAbortTimeout(
    u,
    {
      method: "POST",
      body: fd,
      headers: {
        ...(options.idempotencyKey
          ? { "Idempotency-Key": options.idempotencyKey }
          : {}),
        "X-Capture-Device-Id": deviceId,
        "X-Capture-Segment-Id": segmentId,
      },
    },
    options.timeoutMs ?? CAPTURE_UPLOAD_TIMEOUT_MS,
    options.signal,
  );
  const parsed = await asJson<unknown>(r);
  return normalizeCaptureChunkResponse(parsed);
}

export async function getCaptureControl(
  options: ApiReadOptions = {},
): Promise<CaptureControl> {
  const response = await fetchWithAbortTimeout(
    await apiUrl("/capture/control"),
    { method: "GET" },
    DEFAULT_PROBE_TIMEOUT_MS,
    options.signal,
  );
  return normalizeCaptureControl(await asJson<unknown>(response));
}

export async function getCaptureDevices(
  options: ApiReadOptions = {},
): Promise<CaptureControlSnapshot> {
  const response = await fetchWithAbortTimeout(
    await apiUrl("/capture/devices"),
    { method: "GET" },
    DEFAULT_PROBE_TIMEOUT_MS,
    options.signal,
  );
  const body = await asJson<unknown>(response);
  const record = body !== null && typeof body === "object"
    ? body as Record<string, unknown>
    : {};
  return {
    control: normalizeCaptureControl(record.control ?? body),
    devices: normalizeCaptureDevices(body),
  };
}

export async function updateCaptureControl(input: {
  mode: CaptureMode;
  selectedDeviceIds: string[];
  expectedRevision: number;
}): Promise<CaptureControl> {
  const response = await fetchWithAbortTimeout(
    await apiUrl("/capture/control"),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
    DEFAULT_PROBE_TIMEOUT_MS,
  );
  return normalizeCaptureControl(await asJson<unknown>(response));
}

export async function authorizeCaptureControl(input: {
  deviceId: string;
  revision: number;
}): Promise<{ allowed: boolean; mode: CaptureMode; revision: number }> {
  const response = await fetchWithAbortTimeout(
    await apiUrl("/capture/control/authorize"),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
    DEFAULT_PROBE_TIMEOUT_MS,
  );
  const body = await asJson<Record<string, unknown>>(response);
  return {
    allowed: body.allowed === true,
    mode: body.mode === "multi" ? "multi" : "single",
    revision:
      typeof body.revision === "number" ? body.revision : input.revision,
  };
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
  repeat_dropped: number;
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
  last_audio_stored_at?: string | null;
  /** Process-lifetime admission observation fields; null means legacy backend. */
  observed_audio_frames?: number | null;
  accepted_speech_frames?: number | null;
  accepted_speech_ratio?: number | null;
  /** Monotonic stats cursor; null means legacy backend. */
  stats_sequence?: number | null;
}

export function normalizeCaptureStats(value: unknown): CaptureStats {
  const body = captureRecord(value);
  return {
    chunks_total: finiteNonNegative(body.chunks_total),
    gated_rms: finiteNonNegative(body.gated_rms),
    gated_low_speech: finiteNonNegative(body.gated_low_speech),
    stt_circuit_open: finiteNonNegative(body.stt_circuit_open),
    stt_failed: finiteNonNegative(body.stt_failed),
    stt_empty: finiteNonNegative(body.stt_empty),
    hallu_dropped: finiteNonNegative(body.hallu_dropped),
    repeat_dropped: finiteNonNegative(body.repeat_dropped),
    diarize_failed: finiteNonNegative(body.diarize_failed),
    diarize_returned_none: finiteNonNegative(body.diarize_returned_none),
    stored: finiteNonNegative(body.stored),
    last_chunk_at: nullableString(body.last_chunk_at),
    last_stored_at: nullableString(body.last_stored_at),
    last_audio_stored_at: nullableString(body.last_audio_stored_at),
    last_rms: finiteNonNegative(body.last_rms),
    last_speech_ratio: finiteNonNegative(body.last_speech_ratio),
    last_gate_reason: nullableString(body.last_gate_reason),
    observed_audio_frames: nullableFiniteNonNegative(body.observed_audio_frames),
    accepted_speech_frames: nullableFiniteNonNegative(body.accepted_speech_frames),
    accepted_speech_ratio: nullableFiniteNonNegative(body.accepted_speech_ratio),
    stats_sequence: nullableFiniteNonNegative(body.stats_sequence),
  };
}

export async function getCaptureStats(
  options: ApiReadOptions = {},
): Promise<CaptureStats> {
  const u = await apiUrl("/capture/stats");
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  const parsed = await asJson<unknown>(r);
  return normalizeCaptureStats(parsed);
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
  const r = await fetchWithAbortTimeout(
    u,
    { method: "POST", body: fd },
    CAPTURE_UPLOAD_TIMEOUT_MS,
  );
  return asJson<TranscriptSegment[]>(r);
}

export async function finalizeMeeting(
  meetingId: string,
  title: string,
  options: ApiReadOptions = {},
): Promise<MeetingMinutes> {
  const fd = new FormData();
  fd.append("title", title);
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/finalize`);
  const r = await fetchWithAbortTimeout(
    u,
    { method: "POST", body: fd },
    MEETING_FINALIZE_TIMEOUT_MS,
    options.signal,
  );
  const minutes = await asJson<MeetingMinutes>(r);
  enqueueSyncOperation({
    operation_id: makeOperationId("meeting_summary", meetingId),
    device_id: ensureSyncDeviceId(),
    entity_type: "meeting_summary",
    entity_id: meetingId,
    base_revision: 0,
    updated_at: minutes.created_at || new Date().toISOString(),
    payload: minutes as unknown as Record<string, unknown>,
  });
  return minutes;
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
  options: ApiReadOptions = {},
): Promise<MeetingMinutes> {
  return finalizeMeeting(meetingId, title, options);
}

// ── 全局会议状态机（PRD：自动开/结 + 手动覆盖） ──────────────────

export async function getCurrentMeeting(
  options: ApiReadOptions = {},
): Promise<MeetingStateSnapshot> {
  const u = await apiUrl("/meetings/current");
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
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

export async function listMeetings(
  limit = 50,
  options: ApiReadOptions = {},
): Promise<MeetingSummary[]> {
  const u = await apiUrl(`/meetings?limit=${limit}`);
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  return asJson<MeetingSummary[]>(r);
}

export async function getMeetingTranscript(
  meetingId: string,
  options: ApiReadOptions = {},
): Promise<TranscriptSegment[]> {
  const u = await apiUrl(
    `/meetings/${encodeURIComponent(meetingId)}/transcript`,
  );
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  return asJson<TranscriptSegment[]>(r);
}

/** 拿不到（404 / 未生成）时返回 null，调用方据此显示"暂无纪要"。 */
export async function getMeetingMinutes(
  meetingId: string,
  options: ApiReadOptions = {},
): Promise<MeetingMinutes | null> {
  const u = await apiUrl(`/meetings/${encodeURIComponent(meetingId)}/minutes`);
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  if (r.status === 404) return null;
  return asJson<MeetingMinutes>(r);
}

export async function getMeetingArtifacts(
  meetingId: string,
  options: ApiReadOptions = {},
): Promise<GeneratedArtifact[]> {
  const u = await apiUrl(
    `/meetings/${encodeURIComponent(meetingId)}/artifacts`,
  );
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  // 后端通过 artifact_links 返回持久化关联；会议不存在仍返回 404。
  if (r.status === 404) return [];
  return asJson<GeneratedArtifact[]>(r);
}

export async function listArtifacts(
  limit = 500,
  options: ApiReadOptions = {},
): Promise<GeneratedArtifact[]> {
  const u = await apiUrl(`/artifacts?limit=${limit}`);
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
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
  // artifactIds is retained for source compatibility; the backend derives links
  // from owner-scoped artifact_links and returns a narrow, expiring share path.
  void artifactIds;
  const ticketUrl = await apiUrl(
    `/meetings/${encodeURIComponent(meetingId)}/share-ticket`,
  );
  const ticketResponse = await fetch(ticketUrl, { method: "POST" });
  const { path } = await asJson<{ path: string; expires_in_s: number | null }>(
    ticketResponse,
  );
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
  options: ApiReadOptions = {},
): Promise<AmbientSegment[]> {
  const u = await apiUrl(`/capture/recent?limit=${limit}`);
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
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
  retry_of_run_id?: string;
}, options: ApiReadOptions = {}): Promise<GeneratedArtifact> {
  const u = await apiUrl("/artifacts/generate");
  const r = await fetchWithAbortTimeout(
    u,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    },
    ARTIFACT_GENERATE_TIMEOUT_MS,
    options.signal,
  );
  return asJson<GeneratedArtifact>(r);
}

export async function listWorkflowRuns(filters: {
  meeting_id?: string;
  todo_id?: string;
  agent_task_id?: string;
  state?: string;
  limit?: number;
} = {}, options: ApiReadOptions = {}): Promise<WorkflowRunDTO[]> {
  const params = new URLSearchParams();
  if (filters.meeting_id) params.set("meeting_id", filters.meeting_id);
  if (filters.todo_id) params.set("todo_id", filters.todo_id);
  if (filters.agent_task_id) params.set("agent_task_id", filters.agent_task_id);
  if (filters.state) params.set("state", filters.state);
  params.set("limit", String(filters.limit ?? 100));
  const u = await apiUrl(`/workflows/runs?${params.toString()}`);
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  return asJson<WorkflowRunDTO[]>(r);
}

export async function retryWorkflowRun(
  runId: string,
  reason?: string,
): Promise<WorkflowRunDTO> {
  const u = await apiUrl(`/workflows/runs/${encodeURIComponent(runId)}/retry`);
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  return asJson<WorkflowRunDTO>(r);
}

export async function cancelWorkflowRun(
  runId: string,
  reason?: string,
): Promise<WorkflowRunDTO> {
  const u = await apiUrl(`/workflows/runs/${encodeURIComponent(runId)}/cancel`);
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  return asJson<WorkflowRunDTO>(r);
}

const AGENT_DEVICE_ID_KEY = "echodesk.agentDeviceId";

export function agentDeviceId(): string {
  if (typeof window === "undefined") return "desktop";
  try {
    const existing = window.localStorage.getItem(AGENT_DEVICE_ID_KEY);
    if (existing) return existing;
    const next = `desktop-${Math.random().toString(36).slice(2, 10)}`;
    window.localStorage.setItem(AGENT_DEVICE_ID_KEY, next);
    return next;
  } catch {
    return "desktop";
  }
}

export async function createAgentTask(req: {
  text: string;
  title?: string;
  task_kind?: string;
  conversation_id?: string;
  message_id?: string;
  context?: Record<string, unknown>;
  output_contract?: Record<string, unknown>;
}): Promise<AgentTaskCard> {
  const u = await apiUrl("/agents/tasks");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: agentDeviceId(), ...req }),
  });
  return asJson<AgentTaskCard>(r);
}

export async function listAgentTasks(
  limit = 50,
  options: ApiReadOptions = {},
): Promise<AgentTaskCard[]> {
  const u = await apiUrl(
    `/agents/tasks?device_id=${encodeURIComponent(agentDeviceId())}&limit=${limit}`,
  );
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  return asJson<AgentTaskCard[]>(r);
}

export async function listAgentTaskEvents(
  taskId: string,
  afterSeq = 0,
): Promise<{
  task_id: string;
  events: AgentTaskEvent[];
  snapshot: Record<string, unknown>;
  last_seq: number;
}> {
  const u = await apiUrl(
    `/agents/tasks/${encodeURIComponent(taskId)}/events?after_seq=${afterSeq}`,
  );
  const r = await fetch(u, { cache: "no-store" });
  return asJson<{
    task_id: string;
    events: AgentTaskEvent[];
    snapshot: Record<string, unknown>;
    last_seq: number;
  }>(r);
}

export async function cancelAgentTask(taskId: string): Promise<AgentTaskCard> {
  const u = await apiUrl(`/agents/tasks/${encodeURIComponent(taskId)}/cancel`);
  const r = await fetch(u, { method: "POST" });
  return asJson<AgentTaskCard>(r);
}

export async function grantAgentRunnerAndResume(
  resumeTaskId?: string,
): Promise<{ grant: Record<string, unknown>; resumed_task?: AgentTaskCard | null }> {
  const u = await apiUrl("/agents/grants/claude_code");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      device_id: agentDeviceId(),
      workspace_ids: [],
      resume_task_id: resumeTaskId,
    }),
  });
  return asJson<{ grant: Record<string, unknown>; resumed_task?: AgentTaskCard | null }>(r);
}

export function artifactDownloadUrl(artifactId: string): string {
  const value = String(artifactId ?? "").trim();
  if (!value || /[\\/?#]/.test(value) || /^[a-z][a-z\d+.-]*:/i.test(value)) {
    throw new BackendBasePolicyError(
      "产物路径无效，已拒绝跨 origin 访问",
      "artifact_path_invalid",
    );
  }
  const path = `/artifacts/${encodeURIComponent(value)}/download`;
  const role = backendRole();
  if (role === "paired_hub_sync_gateway") {
    throw new BackendBasePolicyError(
      "paired Hub sync gateway 不能承载业务产物",
      "artifact_hub_role_forbidden",
    );
  }
  let base: string | null;
  try {
    base = backendBaseSnapshot();
  } catch (error) {
    if (
      error instanceof BackendBasePolicyError &&
      error.code === "backend_endpoint_unavailable"
    ) {
      throw new BackendBasePolicyError(
        "后端路由快照不可用，已停止产物下载",
        "artifact_backend_snapshot_missing",
      );
    }
    throw error;
  }
  if (base === null) {
    throw new BackendBasePolicyError(
      "后端路由快照不可用，已停止产物下载",
      "artifact_backend_snapshot_missing",
    );
  }
  if (!base) {
    if (!canUseRelativeBackendProxy()) {
      throw new BackendBasePolicyError(
        "后端路由快照不可用，已停止产物下载",
        "artifact_backend_snapshot_missing",
      );
    }
    return `/api${path}`;
  }
  if (role === "public_service" && !base.startsWith("https://")) {
    throw new BackendBasePolicyError(
      "public service 产物地址必须使用 HTTPS",
      "artifact_public_endpoint_invalid",
    );
  }
  if (role === "local_dev_diagnostic" && runtimeMode() === "release") {
    throw new BackendBasePolicyError(
      "release 不允许使用 local dev 产物地址",
      "artifact_local_role_forbidden",
    );
  }

  let resolved: URL;
  try {
    const origin = new URL(base).origin;
    resolved = new URL(path, base);
    if (resolved.origin !== origin || resolved.pathname !== path) {
      throw new Error("artifact path escaped backend origin");
    }
  } catch {
    throw new BackendBasePolicyError(
      "后端产物地址无效，已停止下载",
      "artifact_backend_endpoint_invalid",
    );
  }
  return resolved.toString();
}

export function artifactIdFromDownloadHref(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const pathname = new URL(href, window.location.href).pathname;
    const match = pathname.match(/^\/artifacts\/([^/]+)\/download$/);
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

/**
 * 解析后端 /rag/ask 的 SSE 响应。
 *
 * 协议（见 backend/app/api/retrieval.py _sse）：
 *   - 中间帧：`event: delta` + `{type:"delta", delta:"..."}`
 *   - 成功终帧：`event: done` + 完整 answer / sources / trace
 *   - 失败终帧：`event: error`，必须 reject，不保留部分答案
 *
 * 为了滚动升级，仍接受旧服务端 `[DONE]`，但提前 EOF 绝不视为成功。
 */
export interface RagAskOptions {
  conversationId?: string;
  messageId?: string;
  onMemoryFrame?: (frame: MemoryFramePayload) => void;
}

export async function ragAsk(
  question: string,
  options: RagAskOptions = {},
): Promise<{
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
  const r = await fetchWithAbortTimeout(
    u,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        conversation_id: options.conversationId ?? "default",
        message_id: options.messageId,
      }),
    },
    RAG_QUERY_TIMEOUT_MS,
    undefined,
    SSE_MAX_RESPONSE_BYTES,
  );
  if (!r.ok) {
    await r.body?.cancel().catch(() => undefined);
    throw new Error(`暂时无法生成回答（HTTP ${r.status}）`);
  }
  // 兼容：若后端非 SSE（如 mock 测试返 JSON），直接 json() 解析
  const ct = r.headers.get("content-type") ?? "";
  if (!ct.includes("text/event-stream")) {
    try {
      return (await r.json()) as Awaited<ReturnType<typeof ragAsk>>;
    } catch {
      throw new Error("RAG 响应格式损坏");
    }
  }

  const reader = r.body?.getReader();
  if (!reader) throw new Error("rag/ask: response body unreadable");
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let answer = "";
  let citations: Array<Record<string, unknown>> = [];
  let arbitration = "rag";
  let completed = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let nl: number;
      while ((nl = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        const lines = block.split("\n");
        const eventType = lines
          .find((line) => line.startsWith("event: "))
          ?.slice("event: ".length)
          .trim();
        const dataLine = lines.find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        const payload = dataLine.slice("data: ".length);
        if (payload === "[DONE]") {
          completed = true;
          continue;
        }
        let obj: Record<string, unknown>;
        try {
          obj = JSON.parse(payload) as Record<string, unknown>;
        } catch {
          throw new Error("RAG 响应格式损坏");
        }
        const frameType = typeof obj.type === "string" ? obj.type : eventType;
        if (frameType === "error" || typeof obj.error === "string") {
          throw new Error("暂时无法生成回答，请稍后重试");
        }
        if (
          (eventType === "memory.status" || eventType === "memory.sources") &&
          (obj.type === "memory.status" || obj.type === "memory.sources")
        ) {
          options.onMemoryFrame?.(obj as unknown as MemoryFramePayload);
          continue;
        }
        if (frameType === "done") {
          if (typeof obj.answer === "string") answer = obj.answer;
          const sources = Array.isArray(obj.sources) ? obj.sources : undefined;
          const meta =
            obj.meta && typeof obj.meta === "object"
              ? (obj.meta as Record<string, unknown>)
              : undefined;
          if (sources) citations = sources as Array<Record<string, unknown>>;
          else if (Array.isArray(meta?.citations)) {
            citations = meta.citations as Array<Record<string, unknown>>;
          }
          const trace =
            obj.trace && typeof obj.trace === "object"
              ? (obj.trace as Record<string, unknown>)
              : undefined;
          if (typeof trace?.chosen_source === "string") {
            arbitration = trace.chosen_source;
          } else if (typeof meta?.chosen_source === "string") {
            arbitration = meta.chosen_source;
          }
          completed = true;
          continue;
        }
        if (typeof obj.delta === "string") {
          answer += obj.delta;
        } else if (obj.meta && typeof obj.meta === "object") {
          const meta = obj.meta as Record<string, unknown>;
          if (Array.isArray(meta.citations)) {
            citations = meta.citations as Array<Record<string, unknown>>;
          }
          if (typeof meta.chosen_source === "string") {
            arbitration = meta.chosen_source;
          }
        }
      }
    }
    buf += decoder.decode();
    if (buf.trim()) throw new Error("RAG 响应在完整帧之前中断");
    if (!completed) throw new Error("RAG 响应未完成，请重试");
    if (!answer.trim()) throw new Error("RAG 未返回有效答案");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
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
export interface ChatAskOptions {
  conversationId?: string;
  messageId?: string;
  onMemoryFrame?: (frame: MemoryFramePayload) => void;
}

export async function chatAsk(
  question: string,
  options: ChatAskOptions = {},
): Promise<string> {
  const u = await apiUrl("/chat");
  const r = await apiTransport(
    u,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        conversation_id: options.conversationId ?? "default",
        message_id: options.messageId,
      }),
    },
    {
      maxResponseBytes: SSE_MAX_RESPONSE_BYTES,
      throwHttpErrors: false,
    },
  );
  if (!r.ok) {
    await r.body?.cancel().catch(() => undefined);
    throw new Error(`暂时无法回复（HTTP ${r.status}）`);
  }
  const ct = r.headers.get("content-type") ?? "";
  if (!ct.includes("text/event-stream")) {
    let obj: { answer?: string; delta?: string; content?: string };
    try {
      obj = (await r.json()) as { answer?: string; delta?: string; content?: string };
    } catch {
      throw new Error("Chat 响应格式损坏");
    }
    const answer = obj.answer ?? obj.delta ?? obj.content ?? "";
    if (!answer.trim()) throw new Error("Chat 未返回有效答案");
    return answer;
  }

  const reader = r.body?.getReader();
  if (!reader) throw new Error("chat: response body unreadable");
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let answer = "";
  let completed = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        const lines = block.split("\n");
        const eventLine = lines.find((line) => line.startsWith("event: "));
        const eventType = eventLine?.slice("event: ".length).trim();
        const dataLine = lines.find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        const payload = dataLine.slice("data: ".length);
        if (payload === "[DONE]") {
          completed = true;
          continue;
        }
        let obj: { delta?: string; error?: string; type?: string } & Partial<MemoryFramePayload>;
        try {
          obj = JSON.parse(payload) as { delta?: string; error?: string };
        } catch {
          throw new Error("Chat 响应格式损坏");
        }
        if (eventType === "error" || typeof obj.error === "string") {
          throw new Error("暂时无法回复，请稍后重试");
        }
        if (
          (eventType === "memory.status" || eventType === "memory.sources") &&
          (obj.type === "memory.status" || obj.type === "memory.sources")
        ) {
          options.onMemoryFrame?.(obj as MemoryFramePayload);
          continue;
        }
        if (typeof obj.delta === "string") {
          answer += obj.delta;
        }
      }
    }
    buf += decoder.decode();
    if (buf.trim()) throw new Error("Chat 响应在完整帧之前中断");
    if (!completed) throw new Error("Chat 响应未完成，请重试");
    if (!answer.trim()) throw new Error("Chat 未返回有效答案");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  }

  return answer;
}

interface MemoryRecallWireResult {
  latency_ms: number;
  matches: Array<{
    relevance: number;
    score: number;
    relation: string;
    candidate: {
      candidate_id: string;
      memory_id?: string | null;
      level: "L0" | "L1" | "L2" | "L3";
      kind: string;
      content: string;
      source_ref: string;
      occurred_at: string;
      confidence: number;
      metadata?: Record<string, unknown>;
    };
  }>;
}

export async function memoryRecall(
  text: string,
  conversationId: string,
  messageId?: string,
): Promise<MemoryFramePayload> {
  const u = await apiUrl("/memory/recall");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, conversation_id: conversationId }),
  });
  const result = await asJson<MemoryRecallWireResult>(r);
  const sources: MemorySourceCard[] = result.matches.map((match, offset) => {
    const item = match.candidate;
    const metadata = item.metadata ?? {};
    const titleValue = metadata.title ?? metadata.meeting_title ?? metadata.artifact_name;
    return {
      index: offset + 1,
      candidate_id: item.candidate_id,
      memory_id: item.memory_id,
      level: item.level,
      kind: item.kind,
      title: typeof titleValue === "string" ? titleValue : item.level === "L1" ? "历史会议与产物" : item.level === "L2" ? "长期记忆" : item.level === "L3" ? "个人配置" : "当前上下文",
      excerpt: item.content,
      source_ref: item.source_ref,
      occurred_at: item.occurred_at,
      confidence: item.confidence,
      relevance: match.relevance,
      score: match.score,
      relation: match.relation,
      manageable: item.level === "L2" || item.level === "L3",
    };
  });
  return {
    type: "memory.sources",
    state: sources.length > 0 ? "found" : "empty",
    label: sources.length > 0 ? `找到 ${sources.length} 条相关历史信息` : "未找到相关历史信息",
    conversation_id: conversationId,
    message_id: messageId,
    model_display_name: "qwen3 8b",
    latency_ms: result.latency_ms,
    sources,
  };
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

export async function listRagDocs(
  options: ApiReadOptions = {},
): Promise<RagDocsResponse> {
  const u = await apiUrl("/rag/docs");
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
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

export type WorkspaceCapability =
  | "local-electron"
  | "host-backend"
  | "unavailable";

function hasCompleteLocalWorkspaceBridge(): boolean {
  if (typeof window === "undefined") return false;
  const bridge = window.echo;
  return (
    bridge?.isElectron === true &&
    bridge.isPublicDemo === true &&
    typeof bridge.getLocalWorkspaceStatus === "function" &&
    typeof bridge.addLocalWorkspaceDir === "function" &&
    typeof bridge.removeLocalWorkspaceDir === "function" &&
    typeof bridge.scanLocalWorkspaces === "function" &&
    typeof bridge.clearLocalWorkspaceDocs === "function" &&
    typeof bridge.cancelLocalWorkspaceOperations === "function"
  );
}

function localWorkspaceBridgeMatchesRendererOrigin(): boolean {
  if (typeof window === "undefined") return false;
  const rendererBase = backendBaseSnapshot();
  const bridgeBase = window.echo?.backendHost;
  if (!rendererBase || !bridgeBase) return true;
  try {
    return new URL(rendererBase).origin === new URL(bridgeBase).origin;
  } catch {
    return false;
  }
}

export function workspaceCapability(): WorkspaceCapability {
  if (typeof window === "undefined") return "unavailable";
  if (!isPublicRuntime()) return "host-backend";
  if (
    hasCompleteLocalWorkspaceBridge() &&
    localWorkspaceBridgeMatchesRendererOrigin()
  ) {
    return "local-electron";
  }
  return "unavailable";
}

let workspaceOriginRevision = 0;
let activeLocalWorkspaceContext: ElectronWorkspaceContext | null = null;

if (typeof window !== "undefined") {
  window.addEventListener(BACKEND_ORIGIN_EVENT, () => {
    workspaceOriginRevision += 1;
    const previous = activeLocalWorkspaceContext;
    activeLocalWorkspaceContext = null;
    if (previous && window.echo?.cancelLocalWorkspaceOperations) {
      void window.echo.cancelLocalWorkspaceOperations(previous).catch((error) => {
        console.warn("[workspace] cancel stale origin operations failed", error);
      });
    }
  });
}

interface LocalWorkspaceLease {
  context: ElectronWorkspaceContext;
  revision: number;
}

function staleWorkspaceOriginError(): Error {
  const error = new Error("后端地址已切换，已取消旧工作区操作");
  error.name = "WorkspaceOriginChangedError";
  return error;
}

function missingWorkspaceBridgeError(operation: string): Error {
  return new Error(`桌面工作区 ${operation} 桥接不可用，已拒绝回退到远端文件系统`);
}

function unavailableWorkspaceError(): Error {
  const error = new Error(
    "当前公共客户端不提供服务器目录扫描；可直接拖入文件，或连接自建服务使用工作区目录",
  );
  error.name = "WorkspaceCapabilityUnavailableError";
  return error;
}

function assertLocalWorkspaceLease(lease: LocalWorkspaceLease): void {
  if (
    lease.revision !== workspaceOriginRevision ||
    activeLocalWorkspaceContext?.expectedBackendOrigin !==
      lease.context.expectedBackendOrigin
  ) {
    throw staleWorkspaceOriginError();
  }
}

async function localWorkspaceLease(): Promise<LocalWorkspaceLease> {
  const revision = workspaceOriginRevision;
  const base = await backendBase();
  const endpoint = base || (await apiUrl("/bootstrap"));
  const expectedBackendOrigin = new URL(endpoint, window.location.href).origin;
  if (revision !== workspaceOriginRevision) throw staleWorkspaceOriginError();

  const bridgeHost =
    window.echo?.backendHost ?? (await window.echo?.getBackendHost?.()) ?? null;
  if (revision !== workspaceOriginRevision) throw staleWorkspaceOriginError();
  if (bridgeHost) {
    const mainOrigin = new URL(bridgeHost).origin;
    if (mainOrigin !== expectedBackendOrigin) {
      throw new Error(
        "当前服务地址与桌面安全身份不一致，本机工作区已停用；请恢复安装包绑定的服务地址",
      );
    }
  }

  const context = { expectedBackendOrigin };
  activeLocalWorkspaceContext = context;
  return { context, revision };
}

async function localWorkspaceLeaseIfEnabled(): Promise<LocalWorkspaceLease | null> {
  const capability = workspaceCapability();
  if (capability === "unavailable") throw unavailableWorkspaceError();
  if (capability === "host-backend") return null;
  return localWorkspaceLease();
}

export async function workspacePickDirectory(options: {
  defaultPath?: string;
} = {}): Promise<string | null> {
  const capability = workspaceCapability();
  if (capability === "unavailable") {
    throw unavailableWorkspaceError();
  }
  // Electron must always use the native directory picker. window.prompt() is
  // unavailable in packaged Electron and previously made local/self-hosted
  // "添加目录" fail before a path was selected. The main process returns an
  // absolute path only for the trusted local runtime; public mode still gets
  // an origin-bound opaque handle.
  if (capability === "host-backend") {
    if (
      typeof window !== "undefined" &&
      window.echo?.isElectron === true &&
      window.echo.pickDirectory
    ) {
      const lease = await localWorkspaceLease();
      const result = await window.echo.pickDirectory(lease.context, options);
      assertLocalWorkspaceLease(lease);
      return result;
    }
    const entered = window.prompt(
      "输入要加入工作区的目录绝对路径（如 /Users/you/Documents）：",
      options.defaultPath ?? "",
    );
    return entered && entered.trim() ? entered.trim() : null;
  }
  if (
    typeof window === "undefined" ||
    window.echo?.isElectron !== true ||
    !window.echo.pickDirectory
  ) {
    return null;
  }
  const lease = await localWorkspaceLease();
  assertLocalWorkspaceLease(lease);
  const result = await window.echo.pickDirectory(lease.context, options);
  assertLocalWorkspaceLease(lease);
  return result;
}

export async function workspaceStatus(
  options: ApiReadOptions = {},
): Promise<WorkspaceStatus> {
  const lease = await localWorkspaceLeaseIfEnabled();
  if (lease) {
    if (!window.echo?.getLocalWorkspaceStatus) {
      throw missingWorkspaceBridgeError("状态读取");
    }
    if (options.signal?.aborted) throw options.signal.reason;
    const result = await window.echo.getLocalWorkspaceStatus(lease.context);
    assertLocalWorkspaceLease(lease);
    if (options.signal?.aborted) throw options.signal.reason;
    return result;
  }
  const u = await apiUrl("/workspace/status");
  const r = await fetch(u, { cache: "no-store", signal: options.signal });
  return asJson<WorkspaceStatus>(r);
}

export async function workspaceScan(): Promise<WorkspaceScanResult> {
  const lease = await localWorkspaceLeaseIfEnabled();
  if (lease) {
    if (!window.echo?.scanLocalWorkspaces) {
      throw missingWorkspaceBridgeError("扫描");
    }
    const result = await window.echo.scanLocalWorkspaces(lease.context);
    assertLocalWorkspaceLease(lease);
    return result;
  }
  const u = await apiUrl("/workspace/scan");
  const r = await fetchWithAbortTimeout(
    u,
    { method: "POST" },
    WORKSPACE_SCAN_TIMEOUT_MS,
  );
  return asJson<WorkspaceScanResult>(r);
}

export async function workspaceClear(): Promise<{ n_removed: number }> {
  const lease = await localWorkspaceLeaseIfEnabled();
  if (lease) {
    if (!window.echo?.clearLocalWorkspaceDocs) {
      throw missingWorkspaceBridgeError("清理");
    }
    const result = await window.echo.clearLocalWorkspaceDocs(lease.context);
    assertLocalWorkspaceLease(lease);
    return result;
  }
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
  const lease = await localWorkspaceLeaseIfEnabled();
  if (lease) {
    if (!window.echo?.addLocalWorkspaceDir) {
      throw missingWorkspaceBridgeError("添加目录");
    }
    const result = await window.echo.addLocalWorkspaceDir(lease.context, path);
    assertLocalWorkspaceLease(lease);
    if (result.added && window.echo.scanLocalWorkspaces) {
      void window.echo
        .scanLocalWorkspaces(lease.context)
        .then(() => assertLocalWorkspaceLease(lease))
        .catch((e) => {
          if (lease.revision === workspaceOriginRevision) {
            console.warn("[workspace] background local scan failed", e);
          }
        });
    }
    return result;
  }
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
  const lease = await localWorkspaceLeaseIfEnabled();
  if (lease) {
    if (!window.echo?.removeLocalWorkspaceDir) {
      throw missingWorkspaceBridgeError("移除目录");
    }
    const result = await window.echo.removeLocalWorkspaceDir(lease.context, path);
    assertLocalWorkspaceLease(lease);
    return result;
  }
  const u = await apiUrl("/workspace/remove-dir");
  const r = await fetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return asJson(r);
}

// ── Desktop Hub pairing / device management ──────────────────────

export interface HubDeviceDTO {
  device_id: string;
  name: string | null;
  platform: string | null;
  status: string | null;
  is_current: boolean;
  last_seen_at: string | null;
}

export interface HubStatusDTO {
  enabled: boolean;
  configured: boolean;
  device_id: string;
  paired: boolean;
  connection:
    | "disabled"
    | "pairing_required"
    | "connecting"
    | "connected"
    | "disconnected"
    | "error";
  pairing_code: string | null;
  pairing_expires_at: string | null;
  devices: HubDeviceDTO[];
  last_sync_at: string | null;
  last_connected_at: string | null;
  last_error: string | null;
}

export interface HubPairingDTO {
  pairing_code: string;
  expires_at: string | null;
}

export async function hubStatus(): Promise<HubStatusDTO> {
  const u = await apiUrl("/hub/status");
  const r = await fetch(u, { cache: "no-store" });
  return asJson<HubStatusDTO>(r);
}

export async function hubCreatePairing(): Promise<HubPairingDTO> {
  const u = await apiUrl("/hub/pairings");
  const r = await fetch(u, { method: "POST" });
  return asJson<HubPairingDTO>(r);
}

export async function hubDevices(): Promise<HubDeviceDTO[]> {
  const u = await apiUrl("/hub/devices");
  const r = await fetch(u, { cache: "no-store" });
  const payload = await asJson<{ items?: HubDeviceDTO[] }>(r);
  return Array.isArray(payload.items) ? payload.items : [];
}

export async function hubRevokeDevice(deviceId: string): Promise<void> {
  const u = await apiUrl(`/hub/devices/${encodeURIComponent(deviceId)}`);
  const r = await fetch(u, { method: "DELETE" });
  if (!r.ok) {
    await r.body?.cancel().catch(() => undefined);
    throw new Error(`EchoDesk Hub 请求失败（HTTP ${r.status}）`);
  }
}

// ── TTS ─────────────────────────────────────────────────────

/**
 * 后端 /tts/speak 失败时抛出的结构化错误。
 *
 * 与裸 Error 的区别：只携带稳定分类码，不保留或转发 backend 原始错误体。
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

function summarizeTtsDetail(
  status: number,
  raw: string,
): { message: string; code: string } {
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
    return {
      message: "语音播报未检测到可播放的声音，请稍后重试",
      code: "tts_silent_output",
    };
  }
  if (detail.startsWith("tts_upstream_error")) {
    return {
      message: "语音播报服务暂时不可用，请稍后重试",
      code: "tts_upstream_error",
    };
  }
  if (status === 503) {
    return { message: "语音播报已在设置中关闭", code: "tts_disabled" };
  }
  if (status === 400) {
    return { message: "没有可播报的内容", code: "tts_invalid_input" };
  }
  return { message: "语音播报失败，请稍后重试", code: `http_${status}` };
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
  options: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<ArrayBuffer> {
  const u = await apiUrl("/tts/speak");
  const r = await fetchWithAbortTimeout(
    u,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice }),
    },
    options.timeoutMs ?? TTS_SPEAK_TIMEOUT_MS,
    options.signal,
    TTS_MAX_RESPONSE_BYTES,
  );
  if (!r.ok) {
    const raw = await r.text();
    const safeFailure = summarizeTtsDetail(r.status, raw);
    throw new TtsSpeakError(safeFailure.message, r.status, safeFailure.code);
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
  const r = await fetchProbe(u, { method: "GET" });
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
  const r = await fetch(u, { cache: "no-store" });
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
