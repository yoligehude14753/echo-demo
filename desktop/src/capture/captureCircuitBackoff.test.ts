import assert from "node:assert/strict";
import test from "node:test";

import { advanceCaptureCircuitWindow } from "./captureCircuitBackoff.ts";

test("同一并发批次的 circuit_open 只升级一次退避窗口", () => {
  const ladder = [60_000, 120_000, 300_000] as const;
  const first = advanceCaptureCircuitWindow(
    { level: -1, openUntilMs: 0 },
    10_000,
    ladder,
  );
  const sameBatch = advanceCaptureCircuitWindow(first, 10_010, ladder);
  assert.deepEqual(first, {
    level: 0,
    openUntilMs: 70_000,
    advanced: true,
  });
  assert.deepEqual(sameBatch, {
    level: 0,
    openUntilMs: 70_000,
    advanced: false,
  });
});

test("退避结束后的下一轮探测只升一级", () => {
  const ladder = [60_000, 120_000, 300_000] as const;
  const next = advanceCaptureCircuitWindow(
    { level: 0, openUntilMs: 70_000 },
    70_000,
    ladder,
  );
  assert.deepEqual(next, {
    level: 1,
    openUntilMs: 190_000,
    advanced: true,
  });
});
