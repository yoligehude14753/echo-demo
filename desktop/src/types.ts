export type BusinessEventType =
  | "meeting.started"
  | "meeting.segment"
  | "meeting.ended"
  | "minutes.ready"
  | "artifact.generating"
  | "artifact.ready"
  | "artifact.failed"
  | "rag.query"
  | "rag.answer.delta"
  | "rag.answer.done"
  | "chat.delta"
  | "chat.done"
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

export interface GeneratedArtifact {
  artifact_id: string;
  artifact_type: string;
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
  | "summarize_meeting"
  | "start_meeting"
  | "end_meeting"
  | "chat";

export interface IntentResult {
  kind: IntentKind;
  confidence: number;
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
  artifacts: GeneratedArtifact[];
  started_at?: string;
  ended_at?: string;
}
