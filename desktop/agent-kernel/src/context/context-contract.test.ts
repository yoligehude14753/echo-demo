import assert from "node:assert/strict";
import { test } from "node:test";
import {
  checkTokenBudget,
  createBudgetTracker,
  parseTokenBudget,
  runContextTurn,
  validateCheckpointPayload,
  verifyCheckpointChecksum,
  ContextCheckpointError,
} from "./index.ts";
import {
  buildContinuationMessage,
  createBriefPayload,
  createSummaryPayload,
} from "../summary/index.ts";
import {
  microCompactMessages,
  TIME_BASED_MC_CLEARED_MESSAGE,
} from "../compact/index.ts";
import type { CanonicalMessage } from "./types.ts";

const NOW = "2026-07-15T01:00:00.000Z";

function messages(): CanonicalMessage[] {
  return [
    {
      messageId: "assistant-1",
      role: "assistant",
      content: [{ type: "tool_use", toolUseId: "tool-1", name: "Read", input: {} }],
    },
    {
      messageId: "user-1",
      role: "user",
      content: [
        {
          type: "tool_result",
          toolUseId: "tool-1",
          result: { content: "old result ".repeat(40), isError: false },
        },
      ],
    },
    {
      messageId: "assistant-2",
      role: "assistant",
      content: [{ type: "tool_use", toolUseId: "tool-2", name: "Write", input: {} }],
    },
    {
      messageId: "user-2",
      role: "user",
      content: [
        {
          type: "tool_result",
          toolUseId: "tool-2",
          result: { content: "recent result", isError: false },
        },
      ],
    },
  ];
}

test("budget parser and continuation preserve the fixed 90/500/3 semantics", () => {
  assert.equal(parseTokenBudget("+500k"), 500_000);
  assert.equal(parseTokenBudget("use 2M tokens"), 2_000_000);
  assert.equal(parseTokenBudget("ordinary text"), null);

  const tracker = createBudgetTracker(1000);
  assert.equal(checkTokenBudget(tracker, 1000, 100, 1100).action, "continue");
  assert.equal(checkTokenBudget(tracker, 1000, 200, 1200).action, "continue");
  assert.equal(checkTokenBudget(tracker, 1000, 300, 1300).action, "continue");
  const decision = checkTokenBudget(tracker, 1000, 400, 1400);
  assert.equal(decision.action, "stop");
  assert.equal(decision.completionEvent?.diminishingReturns, true);
});

test("micro-compact clears old compactable tool results and keeps the recent one", () => {
  const original = messages();
  const result = microCompactMessages(original, { keepRecent: 1 });
  assert.equal(result.changed, true);
  assert.deepEqual(result.clearedToolUseIds, ["tool-1"]);
  assert.equal(
    (result.messages[1]!.content[0] as { result: { content: string } }).result.content,
    TIME_BASED_MC_CLEARED_MESSAGE,
  );
  assert.equal(
    (result.messages[3]!.content[0] as { result: { content: string } }).result.content,
    "recent result",
  );
  assert.equal(
    (original[1]!.content[0] as { result: { content: string } }).result.content,
    "old result ".repeat(40),
  );
});

test("Brief and Summary produce side-effect-free continuation payloads", async () => {
  const brief = createBriefPayload(
    { message: "done", status: "normal", attachments: ["report.txt"] },
    NOW,
    [{ path: "report.txt", size: 12, isImage: false }],
  );
  assert.equal(brief.message, "done");
  assert.equal(brief.attachments?.[0]?.size, 12);

  const summary = await createSummaryPayload(
    "<analysis>scratch</analysis>\n<summary>Work is complete.</summary>",
    NOW,
    true,
  );
  assert.equal(summary.summary, "Summary:\nWork is complete.");
  assert.match(summary.summaryHash, /^[a-f0-9]{64}$/);
  assert.match(
    buildContinuationMessage(summary.summary, { suppressFollowUpQuestions: true }),
    /Continue the conversation from where it left off/,
  );
});

test("production context turn runs compact to summary to checkpoint and fails closed", async () => {
  const turn = await runContextTurn({
    messages: messages(),
    keepRecentToolResults: 1,
    rawSummary: "<summary>Context boundary reached.</summary>",
    recentMessagesPreserved: true,
    checkpointId: "checkpoint-1",
    taskId: "task-1",
    operationKey: "operation-1",
    modelConfigRevision: 7,
    grantRevision: 3,
    lastDurableEventSeq: 12,
    budgetState: {
      turnsUsed: 1,
      toolCallsUsed: 2,
      modelInputTokens: 30,
      modelOutputTokens: 10,
    },
    createdAt: NOW,
  });
  const { checkpoint } = turn;
  assert.equal(turn.summary.summary, "Summary:\nContext boundary reached.");
  assert.equal(checkpoint.compactState.strategy, "microcompact");
  assert.deepEqual(checkpoint.compactState.clearedToolUseIds, ["tool-1"]);
  await verifyCheckpointChecksum(checkpoint);
  await validateCheckpointPayload(checkpoint, {
    taskId: "task-1",
    operationKey: "operation-1",
    modelConfigRevision: 7,
    grantRevision: 3,
    currentDurableEventSeq: 12,
    now: NOW,
    grantExpiresAt: "2026-07-15T02:00:00.000Z",
  });

  await assert.rejects(
    verifyCheckpointChecksum({ ...checkpoint, checksum: "0".repeat(64) }),
    (error: unknown) => error instanceof ContextCheckpointError && error.code === "CHECKPOINT_CORRUPT",
  );
  await assert.rejects(
    validateCheckpointPayload(checkpoint, {
      taskId: "other-task",
      operationKey: "operation-1",
      modelConfigRevision: 7,
      grantRevision: 3,
      currentDurableEventSeq: 12,
      now: NOW,
      grantExpiresAt: "2026-07-15T02:00:00.000Z",
    }),
    (error: unknown) => error instanceof ContextCheckpointError && error.code === "CHECKPOINT_TASK_MISMATCH",
  );
  await assert.rejects(
    validateCheckpointPayload(checkpoint, {
      taskId: "task-1",
      operationKey: "operation-1",
      modelConfigRevision: 7,
      grantRevision: 3,
      currentDurableEventSeq: 11,
      now: NOW,
      grantExpiresAt: "2026-07-15T02:00:00.000Z",
    }),
    (error: unknown) => error instanceof ContextCheckpointError && error.code === "CHECKPOINT_EVENT_SEQ_AHEAD",
  );
  await assert.rejects(
    validateCheckpointPayload(checkpoint, {
      taskId: "task-1",
      operationKey: "operation-1",
      modelConfigRevision: 7,
      grantRevision: 3,
      currentDurableEventSeq: 12,
      now: NOW,
      grantExpiresAt: NOW,
    }),
    (error: unknown) => error instanceof ContextCheckpointError && error.code === "GRANT_EXPIRED",
  );
});
