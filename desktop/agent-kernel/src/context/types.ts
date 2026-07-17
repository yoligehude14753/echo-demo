import type {
  CanonicalContentBlock,
  CanonicalMessage,
} from "../../core/types.ts";

export type { CanonicalContentBlock, CanonicalMessage } from "../../core/types.ts";

export const CONTEXT_SCHEMA_VERSION = 1 as const;

export type ContextCompactStrategy = "none" | "microcompact";

export type ContextCompactState = {
  schemaVersion: 1;
  strategy: ContextCompactStrategy;
  summaryHash: string | null;
  messageCountAtBoundary: number;
  clearedToolUseIds: string[];
};

export type ContextBudgetState = {
  turnsUsed: number;
  toolCallsUsed: number;
  modelInputTokens: number;
  modelOutputTokens: number;
};

export type ContextCheckpointPayload = {
  schemaVersion: 1;
  checkpointId: string;
  taskId: string;
  operationKey: string;
  modelConfigRevision: number;
  grantRevision: number;
  lastDurableEventSeq: number;
  messages: CanonicalMessage[];
  compactState: ContextCompactState;
  budgetState: ContextBudgetState;
  createdAt: string;
  checksum: string;
};

export type ContextCheckpointBody = Omit<ContextCheckpointPayload, "checksum">;

export type CheckpointIdentity = {
  taskId: string;
  operationKey: string;
  modelConfigRevision: number;
  grantRevision: number;
};

export type CheckpointValidationContext = CheckpointIdentity & {
  currentDurableEventSeq: number;
  now?: string;
  grantExpiresAt?: string;
};

export type BudgetTracker = {
  continuationCount: number;
  lastDeltaTokens: number;
  lastGlobalTurnTokens: number;
  startedAt: number;
};

export type BudgetSettings = {
  completionThreshold: number;
  diminishingDeltaTokens: number;
  diminishingAfterContinuations: number;
};

export type ContinueDecision = {
  action: "continue";
  nudgeMessage: string;
  continuationCount: number;
  pct: number;
  turnTokens: number;
  budget: number;
};

export type StopDecision = {
  action: "stop";
  completionEvent: {
    continuationCount: number;
    pct: number;
    turnTokens: number;
    budget: number;
    diminishingReturns: boolean;
    durationMs: number;
  } | null;
};

export type TokenBudgetDecision = ContinueDecision | StopDecision;

export type BriefStatus = "normal" | "proactive";

export type BriefAttachment = {
  path: string;
  size: number;
  isImage: boolean;
  file_uuid?: string;
};

export type BriefRequest = {
  message: string;
  attachments?: string[];
  status: BriefStatus;
};

export type BriefPayload = {
  schemaVersion: 1;
  message: string;
  status: BriefStatus;
  attachments?: BriefAttachment[];
  sentAt: string;
};

export type SummaryPayload = {
  schemaVersion: 1;
  summary: string;
  summaryHash: string;
  createdAt: string;
  recentMessagesPreserved: boolean;
};

export function cloneCanonicalMessages(messages: readonly CanonicalMessage[]): CanonicalMessage[] {
  return messages.map((message) => ({
    ...message,
    content: message.content.map((block): CanonicalContentBlock => ({ ...block })),
  }));
}
