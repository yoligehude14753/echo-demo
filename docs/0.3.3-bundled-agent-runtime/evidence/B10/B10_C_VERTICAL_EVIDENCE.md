# B10 C — Production Vertical Contract Evidence

- Batch: `B10 AgentTask Durable Cutover`
- Role: `vertical-contract-verification`
- C write set: only this evidence file and
  `backend/tests/unit/test_b10_vertical_contract.py`
- Source scene inspected from B10 head: `d4f1cbd`
- Contract: v1; no contract/version change
- C status: `EVIDENCE_READY`
- Batch verdict: not decided by subagent C

## Real production chain

The source gate now observes the real handler/composition seam; no mock-only
handler is used:

```text
desktop/electron/agent-runtime/bridge/production-composition.ts
  -> createProductionEmbeddedRuntimePort()
  -> EmbeddedRuntimePortServer
  -> createProductionEmbeddedRuntimeCommandHandler()
  -> WorkerManager(factoryModule = production-factory.ts)
  -> worker-entry.ts
  -> createWorkerRuntime()
  -> createKernelWorkerRuntime()
  -> EchoAgentKernel.openSession(OpenSessionInput, KernelDeps)
```

具体证据：

- `createProductionEmbeddedRuntimePort()` constructs the real
  `EmbeddedRuntimePortServer` with the real command handler and starts it.
- The command handler resolves and identity-checks `OpenSessionInput`, creates
  `WorkerManager` with the host-provided manifest and default
  `new URL("./production-factory.ts", import.meta.url)`, opens the worker
  session, forwards `session.runTurn()` events through the framed port, and
  keeps command failures fail-closed without synthetic success.
- `worker-entry.ts` imports the host `factoryModule`, resolves the
  `createWorkerRuntime` export, validates the `OpenSessionInput`, and invokes
  the worker runtime factory.
- `production-factory.ts` defines
  `ProductionKernelDependencies extends KernelDeps`; it constructs the
  `EchoAgentKernel` and calls `createKernelWorkerRuntime(kernel, input, deps)`.
- The default `createWorkerRuntime` path requires `KernelDeps`; when the host
  does not bind those dependencies (or binds a partial object),
  `requireDependencies()` throws `PRODUCTION_DEPENDENCIES_UNBOUND` before
  `EchoAgentKernel.openSession()` can run. There is no default-dependency
  fallback.
- `createKernelWorkerRuntime()` calls
  `EchoAgentKernel.openSession(input, deps)`, preserving the typed
  `OpenSessionInput`/`KernelDeps` boundary.
- `requireDependencies()` validates all eight injected ports and throws
  `PRODUCTION_DEPENDENCIES_UNBOUND` for missing or partial dependencies;
  missing dependencies cannot silently create a partial kernel or synthetic
  terminal.
- The backend `EmbeddedRuntimeBackend` receives framed runtime events over the
  inherited duplex transport; `EmbeddedTaskStreamBridge` feeds the resulting
  event records to `AgentTaskService.record_task_event`, where Echo durable
  sequence, raw identity dedupe, terminal arbitration, and task/Workflow state
  remain authoritative.

## Host seam boundary

B11/B13 retain only the host-owned `factoryModule` and dependency-resolution
seams. They do not add a second worker factory, second kernel control plane,
or alternate runtime/CLI/daemon fallback.

## Focused gate

The existing C source gate validates only the real production composition
surface/source wiring and remains free of fake handler injection. It does not
claim a turn happy-path with default or absent dependencies; that path is
fail-closed by `PRODUCTION_DEPENDENCIES_UNBOUND` until a host binds the full
`ProductionKernelDependencies` seam.

| Command | Result |
|---|---|
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/ruff check backend/tests/unit/test_b10_vertical_contract.py` | `PASS`; output: `All checks passed!` |
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/python -m pytest backend/tests/unit/test_b10_vertical_contract.py -q` | `PASS`; `1 passed in 0.27s` |

The prior `BLOCKED_HANDLER_CONTRACT` state is superseded by this production
source gate result. The gate is source-level chain evidence only; it does not
prove a default-dependency turn happy-path and does not run full regression,
provider smoke, packaging, installation-state, or cross-platform verification.

## Scope audit

- C changed only:
  - `docs/0.3.3-bundled-agent-runtime/evidence/B10/B10_C_VERTICAL_EVIDENCE.md`
- `backend/tests/unit/test_b10_vertical_contract.py` logic was not changed in
  this update.
- The production seam files are A-owned concurrent changes and were preserved;
  C did not modify them.
- No commit was created by C.
