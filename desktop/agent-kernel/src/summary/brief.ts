import type {
  BriefAttachment,
  BriefPayload,
  BriefRequest,
  BriefStatus,
} from "../context/types.ts";

export const BRIEF_TOOL_NAME = "SendUserMessage";
export const LEGACY_BRIEF_TOOL_NAME = "Brief";
export const BRIEF_DESCRIPTION = "Send a message to the user";

export const BRIEF_TOOL_PROMPT =
  "Send a message the user will read. Text outside this tool is visible in the detail view, but most won't open it — the answer lives here.\n\n" +
  "`message` supports markdown. `attachments` takes file paths (absolute or cwd-relative) for images, diffs, logs.\n\n" +
  "`status` labels intent: 'normal' when replying to what they just asked; 'proactive' when you're initiating — a scheduled task finished, a blocker surfaced during background work, you need input on something they haven't asked about. Set it honestly; downstream routing uses it.";

export type BriefAttachmentResolver = (
  paths: readonly string[],
  signal: AbortSignal,
) => Promise<BriefAttachment[]>;

function assertStatus(status: BriefStatus): void {
  if (status !== "normal" && status !== "proactive") {
    throw new TypeError("brief status must be normal or proactive");
  }
}

function cloneAttachments(attachments: readonly BriefAttachment[]): BriefAttachment[] {
  return attachments.map((attachment) => {
    if (
      !attachment.path ||
      !Number.isFinite(attachment.size) ||
      attachment.size < 0 ||
      typeof attachment.isImage !== "boolean"
    ) {
      throw new TypeError("brief attachment metadata is invalid");
    }
    return { ...attachment };
  });
}

export function createBriefPayload(
  request: BriefRequest,
  sentAt: string,
  resolvedAttachments?: readonly BriefAttachment[],
): BriefPayload {
  if (typeof request.message !== "string") {
    throw new TypeError("brief message must be a string");
  }
  assertStatus(request.status);
  if (!Number.isFinite(Date.parse(sentAt))) {
    throw new TypeError("brief sentAt must be an ISO timestamp");
  }
  const attachments = resolvedAttachments ? cloneAttachments(resolvedAttachments) : undefined;
  return {
    schemaVersion: 1,
    message: request.message,
    status: request.status,
    ...(attachments && attachments.length > 0 ? { attachments } : {}),
    sentAt,
  };
}

export async function executeBrief(
  request: BriefRequest,
  resolveAttachments: BriefAttachmentResolver | undefined,
  sentAt: string,
  signal: AbortSignal,
): Promise<BriefPayload> {
  if (request.attachments && request.attachments.length > 0 && !resolveAttachments) {
    throw new TypeError("brief attachments require an injected resolver");
  }
  const resolved =
    request.attachments && request.attachments.length > 0
      ? await resolveAttachments!(request.attachments, signal)
      : undefined;
  return createBriefPayload(request, sentAt, resolved);
}

export function briefToolResultMessage(payload: BriefPayload): string {
  const count = payload.attachments?.length ?? 0;
  return count === 0
    ? "Message delivered to user."
    : `Message delivered to user. (${count} ${count === 1 ? "attachment" : "attachments"} included)`;
}
