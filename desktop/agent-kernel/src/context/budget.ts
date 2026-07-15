import type {
  BudgetSettings,
  BudgetTracker,
  TokenBudgetDecision,
} from "./types.ts";

export const DEFAULT_BUDGET_SETTINGS: Readonly<BudgetSettings> = Object.freeze({
  completionThreshold: 0.9,
  diminishingDeltaTokens: 500,
  diminishingAfterContinuations: 3,
});

const MULTIPLIERS: Readonly<Record<string, number>> = Object.freeze({
  k: 1_000,
  m: 1_000_000,
  b: 1_000_000_000,
});

const SHORTHAND_START_RE = /^\s*\+(\d+(?:\.\d+)?)\s*(k|m|b)\b/i;
const SHORTHAND_END_RE = /\s\+(\d+(?:\.\d+)?)\s*(k|m|b)\s*[.!?]?\s*$/i;
const VERBOSE_RE = /\b(?:use|spend)\s+(\d+(?:\.\d+)?)\s*(k|m|b)\s*tokens?\b/i;
const VERBOSE_RE_G = new RegExp(VERBOSE_RE.source, "gi");

function parseBudgetMatch(value: string, suffix: string): number {
  return parseFloat(value) * (MULTIPLIERS[suffix.toLowerCase()] ?? 1);
}

function assertTokenCount(value: number): void {
  if (!Number.isFinite(value) || value < 0) {
    throw new RangeError("token count must be a finite non-negative number");
  }
}

export function parseTokenBudget(text: string): number | null {
  const startMatch = text.match(SHORTHAND_START_RE);
  if (startMatch) return parseBudgetMatch(startMatch[1]!, startMatch[2]!);
  const endMatch = text.match(SHORTHAND_END_RE);
  if (endMatch) return parseBudgetMatch(endMatch[1]!, endMatch[2]!);
  const verboseMatch = text.match(VERBOSE_RE);
  if (verboseMatch) return parseBudgetMatch(verboseMatch[1]!, verboseMatch[2]!);
  return null;
}

export function findTokenBudgetPositions(
  text: string,
): Array<{ start: number; end: number }> {
  const positions: Array<{ start: number; end: number }> = [];
  const startMatch = text.match(SHORTHAND_START_RE);
  if (startMatch) {
    const offset =
      startMatch.index! +
      startMatch[0].length -
      startMatch[0].trimStart().length;
    positions.push({
      start: offset,
      end: startMatch.index! + startMatch[0].length,
    });
  }
  const endMatch = text.match(SHORTHAND_END_RE);
  if (endMatch) {
    const endStart = endMatch.index! + 1;
    const alreadyCovered = positions.some(
      (position) => endStart >= position.start && endStart < position.end,
    );
    if (!alreadyCovered) {
      positions.push({
        start: endStart,
        end: endMatch.index! + endMatch[0].length,
      });
    }
  }
  for (const match of text.matchAll(VERBOSE_RE_G)) {
    positions.push({ start: match.index, end: match.index + match[0].length });
  }
  return positions;
}

export function createBudgetTracker(startedAt = Date.now()): BudgetTracker {
  if (!Number.isFinite(startedAt)) throw new RangeError("startedAt must be finite");
  return {
    continuationCount: 0,
    lastDeltaTokens: 0,
    lastGlobalTurnTokens: 0,
    startedAt,
  };
}

export function getBudgetContinuationMessage(
  pct: number,
  turnTokens: number,
  budget: number,
): string {
  const format = (value: number): string => new Intl.NumberFormat("en-US").format(value);
  return `Stopped at ${pct}% of token target (${format(turnTokens)} / ${format(budget)}). Keep working — do not summarize.`;
}

function stop(
  tracker: BudgetTracker,
  budget: number,
  turnTokens: number,
  now: number,
  diminishingReturns: boolean,
  pct: number,
): TokenBudgetDecision {
  return {
    action: "stop",
    completionEvent:
      tracker.continuationCount > 0 || diminishingReturns
        ? {
            continuationCount: tracker.continuationCount,
            pct,
            turnTokens,
            budget,
            diminishingReturns,
            durationMs: Math.max(0, now - tracker.startedAt),
          }
        : null,
  };
}

export function checkTokenBudget(
  tracker: BudgetTracker,
  budget: number | null,
  globalTurnTokens: number,
  now = Date.now(),
  settings: Readonly<BudgetSettings> = DEFAULT_BUDGET_SETTINGS,
): TokenBudgetDecision {
  assertTokenCount(globalTurnTokens);
  if (budget === null || !Number.isFinite(budget) || budget <= 0) {
    return { action: "stop", completionEvent: null };
  }
  if (
    !Number.isFinite(now) ||
    settings.completionThreshold <= 0 ||
    settings.completionThreshold > 1 ||
    settings.diminishingDeltaTokens < 0 ||
    settings.diminishingAfterContinuations < 1
  ) {
    throw new RangeError("invalid token budget settings");
  }

  const pct = Math.round((globalTurnTokens / budget) * 100);
  const deltaSinceLastCheck = globalTurnTokens - tracker.lastGlobalTurnTokens;
  const isDiminishing =
    tracker.continuationCount >= settings.diminishingAfterContinuations &&
    deltaSinceLastCheck < settings.diminishingDeltaTokens &&
    tracker.lastDeltaTokens < settings.diminishingDeltaTokens;

  if (!isDiminishing && globalTurnTokens < budget * settings.completionThreshold) {
    tracker.continuationCount += 1;
    tracker.lastDeltaTokens = deltaSinceLastCheck;
    tracker.lastGlobalTurnTokens = globalTurnTokens;
    return {
      action: "continue",
      nudgeMessage: getBudgetContinuationMessage(pct, globalTurnTokens, budget),
      continuationCount: tracker.continuationCount,
      pct,
      turnTokens: globalTurnTokens,
      budget,
    };
  }

  return stop(tracker, budget, globalTurnTokens, now, isDiminishing, pct);
}

export function checkTokenBudgetForAgent(
  tracker: BudgetTracker,
  agentId: string | undefined,
  budget: number | null,
  globalTurnTokens: number,
  now = Date.now(),
  settings: Readonly<BudgetSettings> = DEFAULT_BUDGET_SETTINGS,
): TokenBudgetDecision {
  if (agentId) return { action: "stop", completionEvent: null };
  return checkTokenBudget(tracker, budget, globalTurnTokens, now, settings);
}
