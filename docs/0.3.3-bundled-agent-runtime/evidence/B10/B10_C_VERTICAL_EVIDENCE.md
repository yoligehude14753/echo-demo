# B10 C — Blocked Vertical Contract Evidence

- Batch: `B10 AgentTask Durable Cutover`
- Role: `vertical-contract-verification`
- C write set: only this evidence file and
  `backend/tests/unit/test_b10_vertical_contract.py`
- Current B10 head inspected: `353573049073e99f9e026b3b6377c34b1e6a172e`
- Contract: v1; no contract/version change
- C status: `BLOCKED_HANDLER_CONTRACT`
- Batch verdict: not decided by subagent C

## Exact production gap

The current source contains the framed-port surface but not a production
handler call chain:

- `desktop/electron/agent-runtime/bridge/embedded-runtime-server.ts` contains
  the `EmbeddedRuntimePortServer` class definition.
- `desktop/electron/agent-runtime/index.ts` re-exports that class and the
  `EmbeddedRuntimeCommandHandler` type.
- There is no production `new EmbeddedRuntimePortServer(...)` instantiation.
- The B04K `createWorkerRuntime` concrete implementations are test-owned:
  `desktop/electron/agent-runtime/test/contract/production-worker-factory.mjs`
  and the test fixture
  `desktop/electron/agent-runtime/test/fixtures/kernel-runtime-fixture.ts`.
  `desktop/electron/agent-runtime/pool/worker-manager.ts` only carries the
  configurable string default `"createWorkerRuntime"`; it does not provide a
  production factory export or call chain.
- `backend/app/agents/agentos.py` explicitly aliases
  `AgentOSBackend = EmbeddedRuntimeBackend`; this is a compatibility name for
  the inherited-fd embedded adapter, not an external AgentOS fallback and not
  a C blocker.
- The remaining production gaps are the missing `new
  EmbeddedRuntimePortServer(...)` instance, missing production
  `createWorkerRuntime`/`factoryModule` binding, and missing complete
  `OpenSessionInput`/`KernelDeps` injection into a production worker factory.

Therefore C cannot honestly execute a production vertical turn. The prior
mock-only worker/handler harness was removed and is not evidence of closure.

## Blocked gate

The C test is fail-closed and performs only source-level production wiring
checks. It does not inject a fake handler. The expected result is:

```text
BLOCKED_HANDLER_CONTRACT:
production EmbeddedRuntimePortServer instantiation;
production createWorkerRuntime/factoryModule binding;
production OpenSessionInput/KernelDeps injection
```

Focused commands:

| Command | Result |
|---|---|
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/ruff check backend/tests/unit/test_b10_vertical_contract.py` | `PASS`; output: `All checks passed!` |
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/python -m pytest backend/tests/unit/test_b10_vertical_contract.py -q` | `BLOCKED`; exit 1, `1 failed in 0.23s`; failure: `BLOCKED_HANDLER_CONTRACT: production EmbeddedRuntimePortServer instantiation; production createWorkerRuntime/factoryModule binding; production OpenSessionInput/KernelDeps injection` |
| `python3 -m py_compile backend/tests/unit/test_b10_vertical_contract.py` | `PASS`; exit 0, no output |
| `git diff --check` | `PASS`; exit 0, no output |

The earlier source-scan `UnicodeDecodeError` is resolved: the test reads every
production source snapshot with `errors="replace"`, and the final gate reaches
the intended blocked result above.

## Scope audit

- Modified C paths only:
  - `backend/tests/unit/test_b10_vertical_contract.py`
  - `docs/0.3.3-bundled-agent-runtime/evidence/B10/B10_C_VERTICAL_EVIDENCE.md`
- No production implementation, worker pool, manifest, framed transport,
  migration, packaging, installation, or cross-platform file was modified by
  C.
- No mock-only handler is used to claim production closure.
- No commit was created by C.
