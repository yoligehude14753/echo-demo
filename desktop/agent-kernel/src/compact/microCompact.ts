import type {
  CanonicalContentBlock,
  CanonicalMessage,
} from "../context/types.ts";

export const TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]";

export const DEFAULT_COMPACTABLE_TOOL_NAMES: ReadonlySet<string> = new Set([
  "Read",
  "Bash",
  "Grep",
  "Glob",
  "WebSearch",
  "WebFetch",
  "Edit",
  "Write",
]);

export type MicroCompactOptions = {
  keepRecent: number;
  shouldCompact?: boolean;
  compactableToolNames?: ReadonlySet<string>;
};

export type MicroCompactResult = {
  messages: CanonicalMessage[];
  changed: boolean;
  clearedToolUseIds: string[];
  tokensSaved: number;
};

type ToolUseBlock = Extract<CanonicalContentBlock, { type: "tool_use" }>;
type ToolResultBlock = Extract<CanonicalContentBlock, { type: "tool_result" }>;

function isToolUseBlock(block: CanonicalContentBlock): block is ToolUseBlock {
  return block.type === "tool_use";
}

function isToolResultBlock(block: CanonicalContentBlock): block is ToolResultBlock {
  return block.type === "tool_result";
}

function roughTokenCount(text: string): number {
  return Math.ceil(new TextEncoder().encode(text).byteLength / 3);
}

function collectCompactableToolIds(
  messages: readonly CanonicalMessage[],
  compactableToolNames: ReadonlySet<string>,
): string[] {
  const ids: string[] = [];
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    for (const block of message.content) {
      if (isToolUseBlock(block) && compactableToolNames.has(block.name)) {
        ids.push(block.toolUseId);
      }
    }
  }
  return ids;
}

function cloneBlock(block: CanonicalContentBlock): CanonicalContentBlock {
  if (block.type === "tool_result") {
    return { ...block, result: { ...block.result } };
  }
  if (block.type === "tool_use") {
    return { ...block, input: { ...block.input } };
  }
  return { ...block };
}

function validateOptions(options: MicroCompactOptions): void {
  if (!Number.isFinite(options.keepRecent) || options.keepRecent < 0) {
    throw new RangeError("keepRecent must be a finite non-negative number");
  }
}

/**
 * Content-clearing micro-compact. The caller owns the trigger; this function
 * only performs the proven mutation and returns an immutable-by-convention
 * replacement message list. At least one recent compactable result remains.
 */
export function microCompactMessages(
  messages: readonly CanonicalMessage[],
  options: MicroCompactOptions,
): MicroCompactResult {
  validateOptions(options);
  if (options.shouldCompact === false) {
    return {
      messages: messages.map((message) => ({ ...message, content: message.content.map(cloneBlock) })),
      changed: false,
      clearedToolUseIds: [],
      tokensSaved: 0,
    };
  }

  const compactableToolNames =
    options.compactableToolNames ?? DEFAULT_COMPACTABLE_TOOL_NAMES;
  const compactableIds = collectCompactableToolIds(messages, compactableToolNames);
  const keepRecent = Math.max(1, Math.floor(options.keepRecent));
  const keepSet = new Set(compactableIds.slice(-keepRecent));
  const clearSet = new Set(compactableIds.filter((id) => !keepSet.has(id)));
  if (clearSet.size === 0) {
    return {
      messages: messages.map((message) => ({ ...message, content: message.content.map(cloneBlock) })),
      changed: false,
      clearedToolUseIds: [],
      tokensSaved: 0,
    };
  }

  let tokensSaved = 0;
  const cleared = new Set<string>();
  const compactedMessages = messages.map((message) => {
    let touched = false;
    const content = message.content.map((block) => {
      if (
        message.role === "user" &&
        isToolResultBlock(block) &&
        clearSet.has(block.toolUseId) &&
        block.result.content !== TIME_BASED_MC_CLEARED_MESSAGE
      ) {
        tokensSaved += roughTokenCount(block.result.content);
        cleared.add(block.toolUseId);
        touched = true;
        return {
          ...block,
          result: { ...block.result, content: TIME_BASED_MC_CLEARED_MESSAGE },
        };
      }
      return cloneBlock(block);
    });
    return touched ? { ...message, content } : { ...message, content };
  });

  return {
    messages: compactedMessages,
    changed: cleared.size > 0,
    clearedToolUseIds: [...cleared],
    tokensSaved,
  };
}

export const microcompactMessages = microCompactMessages;
