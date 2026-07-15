# F02 Adapter Contracts v1

- Echo effective compatibility baseline: `492053c53441793c220f3b8e1dd231f1faea6e42`
- Planned parent: `705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`
- Contract source: FUSION gate §5, CONTRACT_FREEZE_V1 §§3, 7-14, and read-only evidence in `fusion-interface-matrix.md`
- Status: `PROPOSED_FOR_F04`; not a production implementation

## A. Adapter boundary

The adapter boundary has two directions:

```text
Claude query/engine messages and SDK events
  <-> ClaudeSemanticAdapter
  <-> EchoAgentKernel ports and immutable snapshots
  <-> EchoAgentEventSink / EchoToolRegistry / EchoContextPort
```

The adapter owns protocol translation only. It must not own task lifecycle, persistent workflow state, credentials, or platform side effects. Echo remains the only task, model, grant, artifact, and event authority.

## B. Input contract

```ts
type ClaudeToEchoTurn = {
  taskId: string;
  operationKey: string;
  conversationId?: string;
  messageId?: string;
  systemPrompt: string;
  userMessage: CanonicalUserContent;
  context: EchoContextEnvelope;
  outputContract: OutputContract;
  deadlineAt: string;
  messages: CanonicalMessage[];
};
```

Rules:

1. `taskId` and `operationKey` come from Echo and are immutable for the session.
2. `AgentIntent.text`, `context`, and `output_contract` are the only RC input values eligible for conversion; `runner_model`, `runner_base_url`, and raw credentials are rejected at the embedded port.
3. The adapter must validate system/user size limits and `deadlineAt < grant.expiresAt` before entering the kernel.
4. Source message order is preserved. A tool result must refer to an existing assistant `tool_use.id`; otherwise emit `MODEL_TOOL_ARGUMENTS_INVALID` or a dedicated correlation error and fail closed.

## C. Model request/stream contract

```ts
type EchoModelRequest = {
  requestId: string;
  taskId: string;
  purpose: ModelPurpose;
  configRevision: number;
  routeId: string;
  model: string;
  system: CanonicalSystemBlock[];
  messages: CanonicalMessage[];
  tools: CanonicalToolDefinition[];
  toolChoice?: CanonicalToolChoice;
  maxOutputTokens: number;
};
```

The adapter maps Claude model stream events to the frozen event family without loss:

| Claude-side semantic | Echo-side event | Required invariant |
|---|---|---|
| stream request starts | `agent.turn.started` / internal request-start | one request ID, one task/operation identity |
| text block delta | `agent.message.delta` | concatenate in source order; no silent overwrite |
| tool-use block start | `agent.tool.requested` | preserve tool id, index, name |
| tool input delta | internal tool accumulator | final input must be a JSON object |
| tool result | `agent.tool.completed` or `agent.tool.failed` | exact `tool_use_id` correlation and `is_error` preservation |
| usage | `agent.turn.completed` payload or usage event | preserve estimated flag and token accounting |
| provider stop | `agent.turn.completed` | stop reason remains machine-readable |
| provider error | `agent.turn.failed` | preserve retryable/non-retryable distinction |

Parallel tool calls are accepted only when the Echo capability registry says `concurrencySafe`; otherwise the adapter serializes or rejects explicitly. It must never silently drop a parallel call.

## D. Tool adapter contract

```ts
type EchoToolCall = {
  taskId: string;
  operationKey: string;
  requestId: string;
  toolUseId: string;
  name: string;
  input: Record<string, unknown>;
};

type EchoToolResult = {
  toolUseId: string;
  output: CanonicalToolResult;
  isError: boolean;
};
```

Rules:

1. `toolUseId` is the sole correlation key; never correlate by name or array position alone.
2. Echo capability checks run before invocation and again at invocation, using the task-bound grant revision.
3. A denied call returns `agent.tool.denied` and a canonical tool result with `isError=true`; it must not be retried as a permission upgrade.
4. Direct Claude filesystem, shell, network, MCP, HOME, settings, or credential access is out of scope for the kernel bundle.
5. The adapter may project tool progress to `agent.tool.started/progress/completed`, but durable side effects belong to Echo tools and artifacts.

## E. Permission/grant contract

```ts
type GrantBoundPermission = {
  grantId: string;
  grantRevision: number;
  taskId: string;
  expiresAt: string;
  decision: "allow" | "deny";
  reasonCode?: string;
};
```

Rules:

1. Claude `allow/deny/ask` is an input semantic, not an authority. The adapter converts it to an Echo `GrantSnapshot` decision.
2. The grant snapshot is immutable after session open. Revocation aborts the running kernel and prevents later tool invocation.
3. `bypassPermissions` and `claude_code_full_access` are not valid embedded permission modes. The current RC occurrence at `backend/app/agents/service.py:605-629` is a compatibility blocker, not an adapter default.
4. A pending UI approval is represented by Echo task state `waiting_permission`; after grant creation, `resume_with_grant` means authorization resubmission, not conversation continuation.

## F. Session/checkpoint/resume contract

```ts
type EchoCheckpoint = {
  schemaVersion: 1;
  checkpointId: string;
  taskId: string;
  operationKey: string;
  modelConfigRevision: number;
  grantRevision: number;
  lastDurableEventSeq: number;
  messages: CanonicalMessage[];
  compactState: CompactState;
  budgetState: BudgetState;
  checksum: string;
};
```

Rules:

1. A checkpoint is valid only when checksum, task/operation identity, model revision, grant validity, and durable event sequence all match.
2. `resume_with_grant()` and startup restore must not be labelled Claude conversation resume until the Claude session/checkpoint closure is present and mapped.
3. Replay uses durable Echo `seq` and raw hash/delivery-key dedupe. A replayed event must not re-run a tool side effect.
4. No checkpoint may contain raw credentials, HOME paths, Claude session files, PID, or temporary ports.

## G. Compact/budget contract

1. Compact trigger, start, summary injection, token usage, and completion are explicit ordered events.
2. Claude’s current order is snip -> microcompact -> context collapse -> autocompact; the adapter must preserve the chosen strategy and report the summary boundary.
3. A compact failure is terminal for the compact operation but not automatically terminal for the task unless the kernel contract says so; the failure reason must remain distinguishable from provider/model failure.
4. Current Echo has no agent compact event/state. F04 must not use `task.message` as a compact substitute.

## H. Cancellation/terminal contract

```text
active -> cancel_requested -> cancelled | cancel_failed
active -> succeeded | failed | timeout
```

Rules:

1. First terminal wins. Echo workflow revision arbitration is authoritative; late worker terminals are audit-only and must not overwrite the durable terminal state.
2. `AbortSignal` cancellation must drain/close in-flight tool execution and preserve synthetic tool results where required by Claude’s message grammar.
3. Timeout, provider error, user cancel, grant revocation, and cancel failure are distinct outcome reasons.
4. `cancel()` is idempotent and safe after a terminal state. Echo cancel outbox operation keys remain stable across retries.

## I. Skills/hooks contract

Skills and hooks require a separate port:

```ts
type EchoSkillHookEvent = {
  taskId: string;
  operationKey: string;
  phase: "pre_tool" | "post_tool" | "pre_compact" | "post_compact" | "stop";
  name: string;
  provenance: string;
  sideEffect: "none" | "capability_tool";
};
```

Until this port has ordered event evidence, skills/hooks are `UNKNOWN_CRITICAL`. The adapter must not turn arbitrary hook output into a user-visible task message.

## J. Contract test obligations for F04

- Verify bidirectional tool ID/result correlation with one tool call.
- Verify permission denial does not invoke a tool and yields a durable denied event.
- Verify cancel racing with success produces exactly one durable terminal state.
- Verify compact/resume checkpoint checksum, revision, and sequence checks.
- Verify unknown source event kinds fail closed or become explicitly debug-only.
- Verify every trace validator failure identifies the missing invariant and source/Echo evidence path.
