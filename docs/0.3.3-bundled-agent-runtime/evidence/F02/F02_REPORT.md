# F02 Interface & Semantic Compatibility Report

## Verdicts and F04 admission

There are two distinct decisions:

- Overall compatibility verdict: `SEMANTIC_BLOCKED`. The current RC is not production-compatible with Claude semantics.
- F04 admission verdict: `F04_READY_WITH_ADAPTERS`. A non-production fake-model/fake-tool spike can proceed for `success`, `one-tool-call`, `cancel`, and `mismatch`, provided F04 implements the adapter-owned fields and invariants listed below.

- `INTERFACE_COMPATIBLE`: **false**. No surface is classified `DIRECT`; the current RC cannot claim drop-in semantic compatibility.
- `ADAPTER_REQUIRED`: **true**. Identity, message/tool correlation, durable ordering, terminal arbitration, and event projection are the intended F04 work.
- `SEMANTIC_BLOCKED`: **true for production compatibility**, not a prohibition on the scoped F04 spike.

The validator proves only that the five existing canonical trace fixtures are structurally well-formed and internally ordered. It does not claim that the current RC has executed them.

### Three-layer reclassification

| Layer | F02 items | F04 consequence |
|---|---|---|
| `HARD_BLOCKS_F04` | None for the scoped fake-model/fake-tool `success`, `one-tool-call`, `cancel`, and `mismatch` traces. | The existing input, tool, stream, sequence, and first-terminal invariants provide enough semantic shape to start; F04 must fail closed on missing fields rather than invent production behavior. |
| `F04_ADAPTER_WORK` | `F02-G02` identity, `F02-G03` message order, `F02-G04` fake model stream, `F02-G05` tool correlation, `F02-G09` durable sequence, `F02-G10` terminal arbitration, `F02-G11/G12` typed error/cancel mapping. | Implement and validate the four trace state machines with fake ports. This is the work F04 is expected to do. |
| `PRODUCTION_ONLY_GAPS` | `F02-G01` source closure, `F02-G06` permission persistence/grant semantics, `F02-G07` durable checkpoint/resume, `F02-G08` compact/budget, `F02-G13` skills/hooks, `F02-G15` config discovery, plus artifact publication. | These remain critical for production admission and later Batch work, but do not block a non-production one-tool loop spike that does not claim them. |

### Is adapter-contracts-v1 sufficient for F04?

Yes as a semantic boundary and no as a complete wire-level test schema. It is sufficient to express the four traces without using external AgentOS, HOME state, real credentials, or production persistence. F04 must pin the following exact fields in its task-owned fixtures/validator; this is adapter work and does not require changing the frozen contract:

1. **Common event envelope:** `schemaVersion`, `eventId`, `seq`, `taskId`, `operationKey`, `requestId`, `event`, `payload`, `source`, `emittedAt`, and `terminal`. `seq` must be contiguous in the fake trace; `eventId` must be unique; `taskId`/`operationKey`/`requestId` must remain stable within one trace.
2. **Canonical message block:** `messageId`, `parentMessageId`, `role`, ordered `content[]`, `toolUseId` when present, and `isError` on a tool result. This makes message order and the one-tool round trip testable instead of relying on UI step labels.
3. **Model request identity:** add `operationKey` to the `EchoModelRequest` fixture (the current type has `requestId` and `taskId` but not `operationKey`), and preserve `stopReason`, `usage`, and `retryable` on terminal model events.
4. **Tool mismatch failure:** `code: MODEL_TOOL_CORRELATION_MISMATCH`, `toolUseId`, `expectedToolUseId` or correlation key, `toolInvoked: false`, `retryable: false`, and a terminal/error event. A result for an unknown or duplicate `toolUseId` must never invoke the fake tool.
5. **Cancel request/terminal:** `cancelRequestId`, `reason` (`user|timeout|provider_error|grant_revoked`), `requestedAt`, `expectedRevision`, and terminal `state`/`reasonCode`. The trace must prove first-terminal-wins and make late terminals audit-only.
6. **Tool call grant context:** `grantId` and `grantRevision` on the fake invocation context, even when F04 uses an allow-all test grant; this prevents the fixture from silently normalizing away the production boundary.

With those fields pinned in F04-owned test fixtures, the answer is `F04_READY_WITH_ADAPTERS`, not `HARD_BLOCKED_F04`. F04 must not promote the result to production compatibility or silently close the production-only gaps.

## Base / Head / Commit

- Planned parent: `705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`
- Effective compatibility baseline: `492053c53441793c220f3b8e1dd231f1faea6e42`
- Baseline delta: `fix(echo-033): align desktop e2e runtime contracts`
- Head: the effective baseline plus this single evidence commit; exact post-commit SHA is reported in the final handoff.
- Commit: the single commit created for this task, with message `docs(agent-fusion): F02 interface semantic compatibility`.

The Echo side was inspected at `492053c53441793c220f3b8e1dd231f1faea6e42`; no checkout, reset, cherry-pick, or product-code change was performed.

## Three subagents

Exactly three non-deriving subagents were created:

1. Feynman (`019f646a-26de-7ae3-b268-fbdac7d2868a`) — Claude contract extractor. Extracted real Claude query/tool/permission/compact/session types and event order from `/Users/yoligehude/Downloads/src`; identified missing source closure.
2. Mencius (`019f646a-263d-7f23-9f86-341220e2c70e`) — Echo contract extractor. Extracted `AgentTaskState`, `AgentTaskRecord`, `EchoTaskEvent`, durable sequencing/dedupe, permission gate, cancellation, and the current external AgentOS boundary from the effective RC.
3. Turing (`019f646a-25af-7d13-a9d4-6cdb1354d05b`) — adapter and trace auditor. Audited the classification matrix and canonical traces, and verified the structural validator and critical-gap behavior.

No subagent created another subagent. The Claude source remained read-only.

## Interface classification

| Surface | Classification | Result |
|---|---|---|
| Turn input | `SEMANTIC_REWRITE` | Build `AgentTurnInput`; Echo task/operation identity is authoritative and runner URL/model/credentials are rejected at the embedded boundary. |
| Canonical message/content order | `SEMANTIC_REWRITE` | Preserve canonical content blocks and tool-result order; current flattened Echo events are only projections. |
| Model request/stream | `SEMANTIC_REWRITE` | Current RC submits to external AgentOS; frozen target requires typed embedded `EchoModelPort` events. |
| Tool definition/dispatch | `LOSSLESS_ADAPTER` for trace; `SEMANTIC_REWRITE` for execution | Preserve `tool_use_id`, input, result, and error; execute only through Echo capability tools. |
| Permission allow/ask/deny | `SEMANTIC_REWRITE` | Claude per-tool decisions must map to immutable Echo `GrantSnapshot`; current `bypassPermissions` path is incompatible. |
| Session/task identity | `LOSSLESS_ADAPTER` for identity; `SEMANTIC_REWRITE` for persistence | Map Claude chain metadata to Echo task/operation identity without creating a second persistence authority. |
| Checkpoint/resume/replay/dedupe | `UNKNOWN_CRITICAL` | Echo replay/dedupe exists, but Claude checkpoint/message closure is absent and resume equivalence is unproven. |
| Token budget/compact | `SEMANTIC_REWRITE` | Compact, summary injection, and token accounting require explicit Echo kernel state/events. |
| Event ordering/sequence | `LOSSLESS_ADAPTER` for ordering; `SEMANTIC_REWRITE` for durability | Assign durable Echo sequence after raw-event dedupe and preserve terminal ordering. |
| Cancel/abort/timeout/provider error | `SEMANTIC_REWRITE` | Map in-process abort to typed Echo cancellation/error state with first-terminal-wins. |
| Skills/hooks | `UNKNOWN_CRITICAL` | Current Echo events lack provenance-bearing skill/hook ports and ordered side-effect semantics. |
| Claude source closure/config discovery | `UNKNOWN_CRITICAL` / `UNSUPPORTED` | Missing Claude modules and forbidden HOME/settings/MCP discovery prevent safe compatibility claims. |

Full evidence is in [fusion-interface-matrix.md](/Users/yoligehude/.codex/worktrees/c1c3/echo/docs/0.3.3-bundled-agent-runtime/evidence/F02/fusion-interface-matrix.md).

## Adapter contracts

The proposed F04-facing contracts are recorded in [adapter-contracts-v1.md](/Users/yoligehude/.codex/worktrees/c1c3/echo/docs/0.3.3-bundled-agent-runtime/evidence/F02/adapter-contracts-v1.md). The mandatory invariants are:

- Echo owns `taskId`, `operationKey`, durable state, grants, artifacts, and event sequence.
- `toolUseId` is the sole tool correlation key; input/result/error cannot be reduced to a step label.
- Model stream deltas preserve request identity, order, tool index, usage, stop reason, and retry/fallback meaning.
- Permission is an immutable, revisioned `GrantSnapshot`; `bypassPermissions` is not an adapter escape hatch.
- Compact/resume requires an explicit checkpoint, summary injection, budget state, and post-resume event order.
- Cancellation, timeout, provider failure, and cancel failure remain distinct; late terminal events are audit-only.
- Unknown event kinds and external configuration discovery fail closed.

## Canonical traces and validator

Generated traces:

- [claude-canonical-turn-trace.jsonl](/Users/yoligehude/.codex/worktrees/c1c3/echo/docs/0.3.3-bundled-agent-runtime/evidence/F02/claude-canonical-turn-trace.jsonl)
- [echo-canonical-turn-trace.jsonl](/Users/yoligehude/.codex/worktrees/c1c3/echo/docs/0.3.3-bundled-agent-runtime/evidence/F02/echo-canonical-turn-trace.jsonl)

Both contain: `success`, `one-tool-call`, `permission-denied`, `cancel`, and `compact/resume`. Each record carries task/operation/request identity, canonical sequence, native event sequence, terminal event, and evidence/gap references.

Task-owned validator:

- [task_owned_validator.py](/Users/yoligehude/.codex/worktrees/c1c3/echo/experiments/fusion-compatibility/F02/task_owned_validator.py)
- [validator-result.json](/Users/yoligehude/.codex/worktrees/c1c3/echo/experiments/fusion-compatibility/F02/validator-result.json)

Command and result:

```text
python3 experiments/fusion-compatibility/F02/task_owned_validator.py --claude docs/0.3.3-bundled-agent-runtime/evidence/F02/claude-canonical-turn-trace.jsonl --echo docs/0.3.3-bundled-agent-runtime/evidence/F02/echo-canonical-turn-trace.jsonl
{"critical_gaps":["F02-G01","F02-G06","F02-G07","F02-G08","F02-G13","F02-G15"],"errors":[],"structural_valid":true,"verdict":"SEMANTIC_BLOCKED"}
```

## Critical production-compatibility gap ledger

The six critical production gaps remain open, but under the three-layer decision above they are not `HARD_BLOCKS_F04` for the scoped fake-model/fake-tool spike:

- `F02-G01`: Claude source closure is incomplete (`types/message.ts`, `query/transitions.ts`, and `sdk/*` are absent).
- `F02-G06`: permission semantics differ; current RC exposes the frozen-contract-forbidden bypass path.
- `F02-G07`: Echo grant resubmission/restart recovery is not proven to resume Claude conversation context.
- `F02-G08`: current Echo agent events have no compact/summary/budget state.
- `F02-G13`: skills/hooks lack a typed, provenance-bearing Echo port and ordered event contract.
- `F02-G15`: source-side permission/config discovery exceeds the frozen embedded allowlist and is unsupported.

The full ledger, including bounded and high-severity surfaces, is in [semantic-gap-ledger.md](/Users/yoligehude/.codex/worktrees/c1c3/echo/docs/0.3.3-bundled-agent-runtime/evidence/F02/semantic-gap-ledger.md).

## Governance and scope notes

- `/Users/yoligehude/Desktop/all/echo/AGENTS.md` was not present; governance was taken from `/Users/yoligehude/Desktop/all/AGENTS.md`, the FactStore contract, and all Cursor rules.
- The required FactStore health check reported the pre-existing observation `events dir not found: /Users/yoligehude/Desktop/all/echo/_state/events`; that path was outside the permitted write roots and was not modified.
- No full test suite, real long-running model, Electron package/install, push, PR, or release operation was run.
- Only `docs/0.3.3-bundled-agent-runtime/evidence/F02/**` and `experiments/fusion-compatibility/F02/**` are in scope for this commit; product code and frozen contracts remain unchanged.
