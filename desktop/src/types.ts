export type BusinessEventType =
  | "meeting.started"
  | "meeting.auto_detected"
  | "meeting.auto_ended"
  | "meeting.state_changed"
  | "meeting.segment"
  | "meeting.ended"
  | "meeting.todo.completed"
  | "meeting.todo.updated"
  | "minutes.ready"
  | "minutes.failed"
  | "artifact.generating"
  | "artifact.ready"
  | "artifact.failed"
  | "workflow.event"
  | "workflow.snapshot"
  | "rag.query"
  | "rag.answer.delta"
  | "rag.answer.done"
  | "chat.delta"
  | "chat.done"
  | "tts.suggested"
  | "agent.task.event"
  | "error";

export type ProtocolEventType =
  | "server_hello"
  | "server_ping"
  | "server_resync"
  | "server_sync"
  | "client_hello"
  | "client_ping";

export type EventType = BusinessEventType | ProtocolEventType;

export interface EchoEvent<T = Record<string, unknown>> {
  type: EventType;
  seq: number;
  stream_epoch?: string | null;
  ts: string;
  meeting_id?: string | null;
  payload: T;
}

export const WS_PROTOCOL_VERSION = "1.0";
export const WS_SERVER_PING_TIMEOUT_MS = 30_000;
export const WS_INACTIVE_RECONNECT_MS = 45_000;

export interface TranscriptSegment {
  text: string;
  start_ms: number;
  end_ms: number;
  speaker_id?: string | null;
  speaker_label?: string | null;
}

export interface MinutesSection {
  heading: string;
  bullets: string[];
}

// M_minutes_refactor：会议待办（替代以前的 action_items 纯字符串列表）
export type TodoKind = "actionable" | "info";
export type TodoStatus =
  | "pending"
  | "running"
  | "failed"
  | "waiting_permission"
  | "done"
  | "cancelled";

export interface TodoItem {
  id: string;
  text: string;
  assignee?: string | null;
  kind: TodoKind;
  status: TodoStatus;
  done_at?: string | null;
  artifact_id?: string | null;
  workflow_run_id?: string | null;
  suggested_command?: string | null;
}

export interface MeetingMinutes {
  meeting_id: string;
  title: string; // ← 现在是 LLM 生成的语义化标题（≤18 字）
  duration_sec: number;
  // 兼容字段：后端仍会返；前端 MinutesBody 不再渲染
  speakers: string[];
  summary: string;
  sections: MinutesSection[];
  decisions: string[];
  // ── M_minutes_refactor：UI 优先渲染 todos ────────────────────
  todos: TodoItem[];
  /** @deprecated 改用 todos；旧后端返回时仍透传，UI 不再展示 */
  action_items: string[];
  raw_transcript_ref?: string | null;
  created_at: string;
}

export type ArtifactType =
  | "html"
  | "pptx"
  | "xlsx"
  | "word"
  | "markdown"
  | "pdf"
  | "txt";

export interface GeneratedArtifact {
  artifact_id: string;
  artifact_type: ArtifactType | string;
  title: string;
  file_path: string;
  mime_type: string;
  size_bytes: number;
  generation_latency_ms: number;
  model: string;
  metadata: Record<string, string>;
  run_id?: string | null;
  links?: Array<Record<string, unknown>>;
}

export type WorkflowState =
  | "pending"
  | "running"
  | "cancel_requested"
  | "succeeded"
  | "failed"
  | "timeout"
  | "cancelled"
  | "cancel_failed";

export interface WorkflowRunDTO {
  run_id: string;
  kind: string;
  source: string;
  state: WorkflowState;
  title?: string | null;
  intent_text: string;
  meeting_id?: string | null;
  todo_id?: string | null;
  agent_task_id?: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error?: string | null;
  timeout_s?: number | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at: string;
}

export interface WorkflowEventDTO {
  run_id: string;
  seq: number;
  event_type: string;
  state: WorkflowState;
  visibility: "user" | "debug" | "hidden";
  message?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export type IntentKind =
  | "search_web"
  | "search_rag"
  | "generate_html"
  | "generate_pptx"
  | "generate_xlsx"
  | "generate_word"
  | "generate_markdown"
  | "generate_pdf"
  | "generate_txt"
  | "summarize_meeting"
  | "agent_task"
  | "chat_no_rag"
  | "chat";

export type MeetingMode = "idle" | "in_meeting";
export type StartReason = "auto" | "manual";

/**
 * 纪要生成生命周期（与后端 ``app.schemas.meeting.MinutesStatus`` 对齐）。
 *
 * - null：会议进行中（state="in_meeting"）或后端尚未尝试 finalize
 * - "generating"：finalize 正在跑（兜底，正常情况会议结束后直接跳到 ok/failed）
 * - "ok"：已成功生成（与 state="finalized" 同步）
 * - "generation_failed"：LLM 失败 / JSON 校验失败；UI 应展示「重试」入口
 */
export type MinutesStatus = "generating" | "ok" | "generation_failed";

export interface MeetingStateSnapshot {
  mode: MeetingMode;
  meeting_id: string | null;
  started_at: string | null;
  started_by: StartReason | null;
  /** 最近一条 meeting 的纪要状态（idle 时；in_meeting 时为 null） */
  minutes_status?: MinutesStatus | null;
  /** generation_failed 时的具体错误（截断后给用户看） */
  minutes_error?: string | null;
}

export interface IntentResult {
  kind: IntentKind;
  // P4-fix（2026-05-28）：confidence 现在可以是 null（纯规则匹配路径），
  // 仅 LLM / 关键字分类器输出的真实 float 才表示有意义的概率；
  // 前端在 confidence==null 时显示 "规则匹配" 而不是虚假的 "置信度 100%"。
  confidence: number | null;
  params: Record<string, unknown>;
  rationale: string;
}

import type { MeetingState } from "@/domain/session";

export type { MeetingState };

export interface MeetingCard {
  meeting_id: string;
  title: string;
  /** M_minutes_refactor：LLM 生成的语义化标题（finalize 之后才有）。
   *  左侧列表显示顺序：display_title > title > meeting_id。 */
  display_title?: string | null;
  state: MeetingState;
  segments: TranscriptSegment[];
  speakers: Set<string>;
  /** GET /meetings 返回的持久化汇总；详情尚未加载时供列表首屏展示。 */
  summary_segment_count: number;
  summary_speaker_count: number;
  minutes?: MeetingMinutes;
  /**
   * 纪要生成状态（仅 state="ended" / "in_meeting" 时有意义；
   * "ok" 通常与 minutes 字段共同存在）。
   */
  minutes_status?: MinutesStatus | null;
  /** generation_failed 时透传后端 minutes_error */
  minutes_error?: string | null;
  artifacts: GeneratedArtifact[];
  started_at?: string;
  ended_at?: string;
}

export interface AgentTaskEvent {
  type: "echo_task_event";
  task_id: string;
  runner_task_id?: string | null;
  conversation_id?: string | null;
  message_id?: string | null;
  seq: number;
  event: string;
  state: string;
  visibility: "user" | "debug" | "hidden";
  title?: string | null;
  text_delta?: string | null;
  message?: string | null;
  step?: Record<string, unknown> | null;
  artifacts: Array<Record<string, unknown>>;
  snapshot: Record<string, unknown>;
  actions: Array<Record<string, unknown>>;
  permission?: Record<string, unknown> | null;
  raw_ref?: string | null;
  ts: string;
}

export interface AgentTaskCard {
  task_id: string;
  runner_task_id?: string | null;
  device_id: string;
  conversation_id?: string | null;
  message_id?: string | null;
  title: string;
  intent_text: string;
  route: string;
  task_kind?: string | null;
  state: string;
  progress_text: string;
  final_text?: string | null;
  error?: string | null;
  artifacts: Array<Record<string, unknown>>;
  snapshot: Record<string, unknown>;
  workflow_run_id?: string | null;
  last_seq: number;
  submitted_at: string;
  finished_at?: string | null;
  timeout_s: number;
}
