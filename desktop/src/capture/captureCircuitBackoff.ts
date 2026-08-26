export interface CaptureCircuitWindow {
  level: number;
  openUntilMs: number;
}

export interface CaptureCircuitAdvance extends CaptureCircuitWindow {
  advanced: boolean;
}

/**
 * 同一并发批次可能一起返回 circuit_open；已打开窗口内的后续响应只归并，
 * 不得重复升级或延长。只有退避结束后的下一轮探测才可升一级。
 */
export function advanceCaptureCircuitWindow(
  current: CaptureCircuitWindow,
  nowMs: number,
  ladderMs: readonly number[],
): CaptureCircuitAdvance {
  if (current.openUntilMs > nowMs || ladderMs.length === 0) {
    return { ...current, advanced: false };
  }
  const level = Math.min(current.level + 1, ladderMs.length - 1);
  return {
    level,
    openUntilMs: nowMs + ladderMs[level],
    advanced: true,
  };
}
