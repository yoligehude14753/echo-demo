import { microCompactMessages } from "../compact/microCompact.ts";
import { createSummaryPayload } from "../summary/summary.ts";
import {
  createCheckpointPayload,
} from "./checkpoint.ts";
import type {
  ContextBudgetState,
  ContextCheckpointPayload,
  ContextCompactState,
  CanonicalMessage,
} from "./types.ts";

export type ContextTurnInput = {
  messages: readonly CanonicalMessage[];
  keepRecentToolResults: number;
  rawSummary: string;
  recentMessagesPreserved?: boolean;
  checkpointId: string;
  taskId: string;
  operationKey: string;
  modelConfigRevision: number;
  grantRevision: number;
  lastDurableEventSeq: number;
  budgetState: ContextBudgetState;
  createdAt: string;
};

export type ContextTurnResult = {
  messages: CanonicalMessage[];
  summary: Awaited<ReturnType<typeof createSummaryPayload>>;
  checkpoint: ContextCheckpointPayload;
};

export async function runContextTurn(input: ContextTurnInput): Promise<ContextTurnResult> {
  const compacted = microCompactMessages(input.messages, {
    keepRecent: input.keepRecentToolResults,
  });
  const summary = await createSummaryPayload(
    input.rawSummary,
    input.createdAt,
    input.recentMessagesPreserved ?? false,
  );
  const compactState: ContextCompactState = {
    schemaVersion: 1,
    strategy: compacted.changed ? "microcompact" : "none",
    summaryHash: summary.summaryHash,
    messageCountAtBoundary: compacted.messages.length,
    clearedToolUseIds: compacted.clearedToolUseIds,
  };
  const checkpoint = await createCheckpointPayload({
    schemaVersion: 1,
    checkpointId: input.checkpointId,
    taskId: input.taskId,
    operationKey: input.operationKey,
    modelConfigRevision: input.modelConfigRevision,
    grantRevision: input.grantRevision,
    lastDurableEventSeq: input.lastDurableEventSeq,
    messages: compacted.messages,
    compactState,
    budgetState: { ...input.budgetState },
    createdAt: input.createdAt,
  });
  return { messages: compacted.messages, summary, checkpoint };
}
