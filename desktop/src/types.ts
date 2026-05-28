export type BusinessEventType =
  | "meeting.started"
  | "meeting.auto_detected"
  | "meeting.auto_ended"
  | "meeting.state_changed"
  | "meeting.segment"
  | "meeting.ended"
  | "minutes.ready"
  | "minutes.failed"
  | "artifact.generating"
  | "artifact.ready"
  | "artifact.failed"
  | "rag.query"
  | "rag.answer.delta"
  | "rag.answer.done"
  | "chat.delta"
  | "chat.done"
  | "tts.suggested"
  | "error";

export type ProtocolEventType =
  | "server_hello"
  | "server_ping"
  | "server_resync"
  | "client_hello"
  | "client_ping";

export type EventType = BusinessEventType | ProtocolEventType;

export interface EchoEvent<T = Record<string, unknown>> {
  type: EventType;
  seq: number;
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

export interface MeetingMinutes {
  meeting_id: string;
  title: string;
  duration_sec: number;
  speakers: string[];
  summary: string;
  sections: MinutesSection[];
  decisions: string[];
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
  state: MeetingState;
  segments: TranscriptSegment[];
  speakers: Set<string>;
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
