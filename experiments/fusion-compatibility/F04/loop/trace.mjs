import {
  BoundedFusionLoop,
  DeterministicFakeToolRegistry,
} from "./loop.mjs";

const CASES = ["success", "cancel", "mismatch"];

function inputFor(caseName) {
  return {
    taskId: `f04-task-${caseName}`,
    operationKey: `f04-op-${caseName}`,
    requestId: `f04-request-${caseName}`,
    grantId: "grant-f04-001",
    grantRevision: 7,
    userText: "Read demo.txt and summarize it",
  };
}
function runCase(caseName) {
  const tools = new DeterministicFakeToolRegistry();
  const loop = new BoundedFusionLoop({ tools });
  loop.startTurn(inputFor(caseName));

  if (caseName === "success") {
    loop.resumeWithToolResult(loop.invokePendingTool());
  } else if (caseName === "cancel") {
    loop.cancel({
      cancelRequestId: "cancel-f04-001",
      reason: "user",
      requestedAt: "2026-01-01T00:00:10.000Z",
      expectedRevision: 1,
    });
    loop.recordLateTerminal({ event: "agent.turn.completed", state: "succeeded" });
  } else if (caseName === "mismatch") {
    loop.rejectMismatch({ toolUseId: "tool-wrong", expectedToolUseId: "tool-1" });
  } else {
    throw new Error(`unknown case: ${caseName}`);
  }

  const trace = loop.trace(caseName);
  validateTrace(trace);
  return trace;
}

function validateTrace(trace) {
  if (!trace.identity.task_id || !trace.identity.operation_key || !trace.identity.request_id) {
    throw new Error(`${trace.case}: identity is incomplete`);
  }
  const events = trace.canonical_events;
  const seqs = events.map((event) => event.seq);
  if (JSON.stringify(seqs) !== JSON.stringify(seqs.map((_, index) => index + 1))) {
    throw new Error(`${trace.case}: sequence is not contiguous`);
  }
  if (new Set(events.map((event) => event.eventId)).size !== events.length) {
    throw new Error(`${trace.case}: event ids are not unique`);
  }
  const identityValues = events.map((event) =>
    `${event.taskId}/${event.operationKey}/${event.requestId}`,
  );
  if (new Set(identityValues).size !== 1) throw new Error(`${trace.case}: identity drift`);
  const last = events.at(-1);
  if (!last.terminal || last.event !== trace.terminal.event) {
    throw new Error(`${trace.case}: terminal is not final`);
  }

  const eventNames = events.map((event) => event.event);
  if (trace.case === "success") {
    if (!eventNames.includes("agent.tool.requested") || !eventNames.includes("agent.tool.completed")) {
      throw new Error("success: tool request/result pair missing");
    }
    if (!eventNames.includes("agent.model.continuation") || trace.terminal.state !== "succeeded") {
      throw new Error("success: continuation or success terminal missing");
    }
    const completed = events.find((event) => event.event === "agent.tool.completed");
    if (completed.payload.toolUseId !== "tool-1" || completed.payload.isError !== false) {
      throw new Error("success: tool result correlation mismatch");
    }
    if (trace.tool_invocations.length !== 1) throw new Error("success: fake tool was not invoked once");
  }
  if (trace.case === "cancel") {
    if (!eventNames.includes("agent.turn.cancel.requested") || trace.terminal.state !== "cancelled") {
      throw new Error("cancel: cancel sequence missing");
    }
    const synthetic = events.find((event) => event.event === "agent.tool.completed");
    if (!synthetic?.payload.synthetic || synthetic.payload.isError !== true) {
      throw new Error("cancel: synthetic correlated tool result missing");
    }
    if (trace.tool_invocations.length !== 0) throw new Error("cancel: cancelled tool was invoked");
    if (!trace.audits.some((audit) => audit.kind === "late_terminal_ignored")) {
      throw new Error("cancel: late terminal was not audit-only");
    }
  }
  if (trace.case === "mismatch") {
    const rejected = events.find((event) => event.event === "agent.tool.rejected");
    if (rejected?.payload.code !== "MODEL_TOOL_CORRELATION_MISMATCH") {
      throw new Error("mismatch: rejection code missing");
    }
    if (rejected.payload.toolInvoked !== false || trace.tool_invocations.length !== 0) {
      throw new Error("mismatch: rejected call invoked the fake tool");
    }
    if (eventNames.includes("agent.tool.completed") || trace.terminal.state !== "failed") {
      throw new Error("mismatch: invalid result was accepted");
    }
  }
}

function summary(traces) {
  return {
    cases: traces.map((trace) => trace.case),
    eventCounts: Object.fromEntries(traces.map((trace) => [trace.case, trace.canonical_events.length])),
    toolInvocations: Object.fromEntries(
      traces.map((trace) => [trace.case, trace.tool_invocations.length]),
    ),
    terminalStates: Object.fromEntries(
      traces.map((trace) => [trace.case, trace.terminal.state]),
    ),
  };
}

const args = process.argv.slice(2);
const verify = args.includes("--verify");
const all = args.includes("--all");
const caseIndex = args.indexOf("--case");
const requested = caseIndex >= 0 ? args[caseIndex + 1] : null;
const selected = all ? CASES : requested ? [requested] : [];

if (selected.length === 0 || selected.some((caseName) => !CASES.includes(caseName))) {
  console.error("usage: node trace.mjs --case success|cancel|mismatch [--verify]");
  console.error("   or: node trace.mjs --all [--verify]");
  process.exitCode = 2;
} else {
  const traces = selected.map(runCase);
  if (verify) {
    console.log(`F04_TRACE_OK ${JSON.stringify(summary(traces))}`);
  } else {
    for (const trace of traces) console.log(JSON.stringify(trace));
  }
}
