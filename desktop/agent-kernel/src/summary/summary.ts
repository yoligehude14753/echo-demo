import { sha256Hex } from "../context/hash.ts";
import type { SummaryPayload } from "../context/types.ts";

export const SUMMARY_NO_TOOLS_PREAMBLE =
  "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n" +
  "The summary is an internal continuation artifact. Preserve concrete work state, decisions, files, errors, and pending tasks.";

export function buildSummaryPrompt(customInstructions?: string): string {
  const prompt = `${SUMMARY_NO_TOOLS_PREAMBLE}

Create a detailed summary of the conversation so far. Use these sections:

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and fixes
5. Problem Solving
6. Pending Tasks
7. Current Work

Return an <analysis> drafting block followed by a <summary> block. The analysis is removed before the summary is persisted.`;
  return customInstructions && customInstructions.trim().length > 0
    ? `${prompt}\n\nAdditional Instructions:\n${customInstructions.trim()}`
    : prompt;
}

export function formatCompactSummary(summary: string): string {
  let formatted = summary.replace(/<analysis>[\s\S]*?<\/analysis>/, "");
  const summaryMatch = formatted.match(/<summary>([\s\S]*?)<\/summary>/);
  if (summaryMatch) {
    const content = summaryMatch[1] ?? "";
    formatted = formatted.replace(
      /<summary>[\s\S]*?<\/summary>/,
      `Summary:\n${content.trim()}`,
    );
  }
  return formatted.replace(/\n\n+/g, "\n\n").trim();
}

export function buildContinuationMessage(
  summary: string,
  options: {
    suppressFollowUpQuestions?: boolean;
    transcriptPath?: string;
    recentMessagesPreserved?: boolean;
  } = {},
): string {
  const formattedSummary = formatCompactSummary(summary);
  let continuation =
    "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\n" +
    formattedSummary;
  if (options.transcriptPath) {
    continuation +=
      `\n\nIf you need specific details from before compaction, read the full transcript at: ${options.transcriptPath}`;
  }
  if (options.recentMessagesPreserved) {
    continuation += "\n\nRecent messages are preserved verbatim.";
  }
  if (options.suppressFollowUpQuestions) {
    continuation +=
      "\n\nContinue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with ‘I'll continue’ or similar. Pick up the last task as if the break never happened.";
  }
  return continuation;
}

export const getCompactUserSummaryMessage = buildContinuationMessage;

export async function createSummaryPayload(
  rawSummary: string,
  createdAt: string,
  recentMessagesPreserved = false,
): Promise<SummaryPayload> {
  if (typeof rawSummary !== "string" || rawSummary.trim().length === 0) {
    throw new TypeError("summary must be a non-empty string");
  }
  if (!Number.isFinite(Date.parse(createdAt))) {
    throw new TypeError("summary createdAt must be an ISO timestamp");
  }
  const summary = formatCompactSummary(rawSummary);
  if (summary.length === 0) throw new TypeError("formatted summary is empty");
  return {
    schemaVersion: 1,
    summary,
    summaryHash: await sha256Hex(summary),
    createdAt,
    recentMessagesPreserved,
  };
}
