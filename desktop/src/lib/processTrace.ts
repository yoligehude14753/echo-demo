import type { EchoEvent, GeneratedArtifact } from "@/types";

export type ProcessTraceKind = "answer" | "artifact";
export type ProcessTraceStatus = "running" | "succeeded" | "failed";
export type ProcessStepState = "pending" | "running" | "done" | "failed";

export interface ProcessTraceStep {
  id: string;
  label: string;
  state: ProcessStepState;
  detail?: string;
  at: string;
}

export interface ProcessTrace {
  id: string;
  kind: ProcessTraceKind;
  title: string;
  status: ProcessTraceStatus;
  startedAt: string;
  updatedAt: string;
  route: string;
  model?: string | null;
  modelEvidence?: "requested" | "observed";
  steps: ProcessTraceStep[];
  error?: string | null;
  artifact?: GeneratedArtifact | null;
}

type MutableTrace = ProcessTrace & {
  stepIndex: Map<string, number>;
};

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function modelFrom(payload: Record<string, unknown>): string | null {
  return (
    text(payload.model_id) ??
    text(payload.model) ??
    text(payload.model_display_name)
  );
}

function artifactFrom(payload: Record<string, unknown>): GeneratedArtifact | null {
  const artifactId = text(payload.artifact_id);
  const artifactType = text(payload.artifact_type);
  if (!artifactId || !artifactType) return null;
  const metadata = asRecord(payload.metadata);
  const links = Array.isArray(payload.links)
    ? payload.links.filter(
        (item): item is Record<string, unknown> =>
          item !== null && typeof item === "object",
      )
    : [];
  return {
    artifact_id: artifactId,
    artifact_type: artifactType,
    title: text(payload.title) ?? "",
    file_path: text(payload.file_path),
    mime_type: text(payload.mime_type) ?? "application/octet-stream",
    size_bytes: numberOr(payload.size_bytes, 0),
    generation_latency_ms: numberOr(payload.generation_latency_ms, 0),
    model: text(payload.model) ?? "",
    metadata: Object.fromEntries(
      Object.entries(metadata).flatMap(([key, value]) => {
        const stringValue = text(value);
        return stringValue === null ? [] : [[key, stringValue]];
      }),
    ),
    run_id: text(payload.run_id),
    links,
  };
}

function artifactLabel(kind: string): string {
  const labels: Record<string, string> = {
    html: "HTML",
    markdown: "Markdown",
    word: "Word",
    xlsx: "Excel",
    pptx: "PPT",
    pdf: "PDF",
    txt: "TXT",
  };
  return labels[kind.toLowerCase()] ?? kind.toUpperCase();
}

function eventInScope(event: EchoEvent, currentMeetingId: string | null): boolean {
  const eventMeetingId = event.meeting_id ?? null;
  return currentMeetingId === null
    ? eventMeetingId === null
    : eventMeetingId === currentMeetingId;
}

function touch(trace: MutableTrace, at: string): void {
  if (at > trace.updatedAt) trace.updatedAt = at;
}

function upsertStep(
  trace: MutableTrace,
  step: Omit<ProcessTraceStep, "at"> & { at?: string },
): void {
  const next = {
    ...step,
    at: step.at ?? trace.updatedAt,
  };
  const index = trace.stepIndex.get(step.id);
  if (index === undefined) {
    trace.stepIndex.set(step.id, trace.steps.length);
    trace.steps.push(next);
    return;
  }
  const previous = trace.steps[index];
  trace.steps[index] = {
    ...previous,
    ...next,
    // A terminal state must not be downgraded by a late heartbeat.
    state:
      previous.state === "failed" || previous.state === "done"
        ? previous.state
        : next.state,
  };
}

function createTrace(
  id: string,
  kind: ProcessTraceKind,
  title: string,
  at: string,
  route: string,
): MutableTrace {
  return {
    id,
    kind,
    title,
    status: "running",
    startedAt: at,
    updatedAt: at,
    route,
    model: null,
    modelEvidence: undefined,
    steps: [],
    error: null,
    artifact: null,
    stepIndex: new Map(),
  };
}

function setModel(
  trace: MutableTrace,
  model: string | null,
  evidence: "requested" | "observed",
): void {
  if (!model) return;
  // An observed artifact response is stronger than the requested setting.
  if (trace.modelEvidence === "observed" && evidence === "requested") return;
  trace.model = model;
  trace.modelEvidence = evidence;
}

function findAnswerTrace(
  traces: Map<string, MutableTrace>,
  messageId: string | null,
  at: string,
): MutableTrace | null {
  if (messageId) {
    const direct = traces.get(`answer:${messageId}`);
    if (direct) return direct;
  }
  const candidates = [...traces.values()]
    .filter((trace) => trace.kind === "answer")
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  return (
    candidates.find((trace) => {
      const delta = Math.abs(new Date(at).getTime() - new Date(trace.updatedAt).getTime());
      return Number.isFinite(delta) && delta <= 5 * 60_000;
    }) ?? null
  );
}

function shouldHideAnswerEvent(payload: Record<string, unknown>): boolean {
  const answer = text(payload.answer) ?? "";
  // CommandBar emits a conversational acknowledgement when an artifact job is
  // submitted. The artifact trace is the authoritative process card, so avoid
  // rendering a duplicate answer trace for that acknowledgement.
  return answer.startsWith("已开始") || answer.startsWith("已生成 ");
}

/**
 * Project the bounded EchoEvent stream into user-visible process traces.
 * Only observed events are rendered; absent model/step data stays absent.
 */
export function deriveProcessTraces(
  events: EchoEvent[],
  currentMeetingId: string | null,
  limit = 8,
): ProcessTrace[] {
  const traces = new Map<string, MutableTrace>();
  const artifactRunIds = new Map<string, string>();

  for (const event of events) {
    if (!eventInScope(event, currentMeetingId)) continue;
    const payload = asRecord(event.payload);
    const at = event.ts;

    if (event.type === "artifact.generating") {
      const runId = text(payload.run_id) ?? `event-${event.seq}`;
      const kind = text(payload.artifact_type) ?? "html";
      const trace =
        traces.get(`artifact:${runId}`) ??
        createTrace(
          `artifact:${runId}`,
          "artifact",
          `生成 ${artifactLabel(kind)}`,
          at,
          `意图 → ${artifactLabel(kind)} → 工作流`,
        );
      traces.set(trace.id, trace);
      artifactRunIds.set(runId, trace.id);
      touch(trace, at);
      setModel(trace, modelFrom(payload), "requested");
      upsertStep(trace, {
        id: "request",
        label: "已接收生成请求",
        state: "done",
        detail: text(payload.brief) ?? undefined,
        at,
      });
      upsertStep(trace, {
        id: "generate",
        label: `生成 ${artifactLabel(kind)}`,
        state: "running",
        at,
      });
      continue;
    }

    if (event.type === "workflow.event") {
      const runId = text(payload.run_id);
      if (!runId) continue;
      const traceId = artifactRunIds.get(runId) ?? `artifact:${runId}`;
      const trace = traces.get(traceId);
      if (!trace) continue;
      touch(trace, at);
      const eventType = text(payload.event_type) ?? "workflow.event";
      const message = text(payload.message) ?? text(asRecord(payload.payload).message);
      const state = text(payload.state);
      if (eventType === "artifact.generating" || eventType === "workflow.started") {
        upsertStep(trace, {
          id: "workflow",
          label: "工作流已启动",
          state: "done",
          detail: message ?? undefined,
          at,
        });
      } else if (state === "failed" || state === "timeout") {
        trace.status = "failed";
        trace.error = message;
        upsertStep(trace, {
          id: "generate",
          label: "生成失败",
          state: "failed",
          detail: message ?? undefined,
          at,
        });
      } else if (state === "succeeded") {
        upsertStep(trace, {
          id: "quality",
          label: "工作流完成，等待产物回执",
          state: "done",
          detail: message ?? undefined,
          at,
        });
      } else if (message) {
        upsertStep(trace, {
          id: `workflow-${eventType}`,
          label: message,
          state: "running",
          at,
        });
      }
      continue;
    }

    if (event.type === "artifact.ready") {
      const artifact = artifactFrom(payload);
      if (!artifact) continue;
      const runId = text(payload.run_id) ?? artifact.run_id ?? `artifact-${artifact.artifact_id}`;
      const traceId = artifactRunIds.get(runId) ?? `artifact:${runId}`;
      const trace =
        traces.get(traceId) ??
        createTrace(
          traceId,
          "artifact",
          `生成 ${artifactLabel(artifact.artifact_type)}`,
          at,
          `工作流 → ${artifactLabel(artifact.artifact_type)}`,
        );
      traces.set(traceId, trace);
      artifactRunIds.set(runId, traceId);
      touch(trace, at);
      trace.status = "succeeded";
      trace.artifact = artifact;
      setModel(trace, artifact.model || null, "observed");
      upsertStep(trace, { id: "request", label: "已接收生成请求", state: "done", at });
      upsertStep(trace, {
        id: "generate",
        label: `生成 ${artifactLabel(artifact.artifact_type)}`,
        state: "done",
        at,
      });
      upsertStep(trace, { id: "quality", label: "产物回执已确认", state: "done", at });
      upsertStep(trace, {
        id: "preview",
        label: artifact.artifact_type.toLowerCase() === "html" ? "动态 HTML 已就绪" : "产物已就绪",
        state: "done",
        detail: artifact.title || undefined,
        at,
      });
      continue;
    }

    if (event.type === "artifact.failed") {
      const runId = text(payload.run_id);
      if (!runId) continue;
      const traceId = artifactRunIds.get(runId) ?? `artifact:${runId}`;
      const kind = text(payload.artifact_type) ?? "html";
      const trace =
        traces.get(traceId) ??
        createTrace(
          traceId,
          "artifact",
          `生成 ${artifactLabel(kind)}`,
          at,
          `工作流 → ${artifactLabel(kind)}`,
        );
      traces.set(traceId, trace);
      artifactRunIds.set(runId, traceId);
      touch(trace, at);
      trace.status = "failed";
      trace.error = text(payload.error) ?? text(payload.message);
      upsertStep(trace, {
        id: "generate",
        label: "生成失败",
        state: "failed",
        detail: trace.error ?? undefined,
        at,
      });
      continue;
    }

    if (event.type === "rag.query") {
      const messageId = text(payload.message_id) ?? `seq-${event.seq}`;
      const trace =
        traces.get(`answer:${messageId}`) ??
        createTrace(`answer:${messageId}`, "answer", "处理对话请求", at, "对话 → 检索 → 回答");
      traces.set(trace.id, trace);
      touch(trace, at);
      upsertStep(trace, {
        id: "request",
        label: "已接收请求",
        state: "done",
        detail: text(payload.question) ?? undefined,
        at,
      });
      continue;
    }

    if (event.type === "memory.status" || event.type === "memory.sources") {
      const messageId = text(payload.message_id);
      const trace = findAnswerTrace(traces, messageId, at);
      if (!trace) continue;
      touch(trace, at);
      setModel(trace, modelFrom(payload), "observed");
      const state = text(payload.state);
      if (event.type === "memory.status" && state === "recalling") {
        upsertStep(trace, { id: "memory", label: "关联历史上下文", state: "running", at });
      } else {
        const count = numberOr(payload.source_count, Array.isArray(payload.sources) ? payload.sources.length : 0);
        upsertStep(trace, {
          id: "memory",
          label: count > 0 ? "历史上下文已关联" : "历史上下文为空",
          state: "done",
          detail: count > 0 ? `${count} 条来源` : undefined,
          at,
        });
      }
      continue;
    }

    if (event.type === "rag.answer.done" || event.type === "chat.done") {
      if (shouldHideAnswerEvent(payload)) continue;
      const messageId = text(payload.message_id);
      const trace = findAnswerTrace(traces, messageId, at);
      if (!trace) continue;
      touch(trace, at);
      setModel(trace, modelFrom(payload), "observed");
      const answer = text(payload.answer);
      if (!answer) {
        trace.status = "failed";
        trace.error = "未收到有效回答";
        upsertStep(trace, { id: "answer", label: "回答未完成", state: "failed", at });
      } else {
        trace.status = "succeeded";
        upsertStep(trace, {
          id: "answer",
          label: "回答已生成",
          state: "done",
          detail: event.type === "rag.answer.done" ? text(payload.arbitration) ?? undefined : undefined,
          at,
        });
      }
    }
  }

  return [...traces.values()]
    .map(({ stepIndex: _stepIndex, ...trace }) => trace)
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
    .slice(0, limit)
    .reverse();
}
