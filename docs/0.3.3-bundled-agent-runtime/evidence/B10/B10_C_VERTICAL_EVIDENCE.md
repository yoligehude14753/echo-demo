# B10 C — Vertical Contract Evidence

- Batch: `B10 AgentTask Durable Cutover`
- Role: `vertical-contract-verification`
- Baseline: `ffbacb9d0ffa1b62a205f98ff437be4219e9ee08`
- Contract: v1; no contract/version change
- C status: `EVIDENCE_READY`
- Batch verdict: not decided by subagent C

## C-owned write set

- `backend/tests/unit/test_b10_vertical_contract.py`
- `docs/0.3.3-bundled-agent-runtime/evidence/B10/B10_C_VERTICAL_EVIDENCE.md`

No production implementation file was changed by C. The test enters the
authoritative `AgentTaskService.record_task_event` sink and uses a deterministic
test-owned worker trace to make raw identity, durable sequence, terminal
arbitration, and workflow projection observable.

## Focused verification

| Command | Result |
|---|---|
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/python -m pytest tests/unit/test_b10_vertical_contract.py -q` (cwd: `backend/`) | `PASS`, `2 passed in 0.57s` |
| `python3 -m py_compile backend/tests/unit/test_b10_vertical_contract.py` | `PASS` |
| `/Users/yoligehude/Desktop/all/echo/backend/.venv/bin/ruff check tests/unit/test_b10_vertical_contract.py` (cwd: `backend/`) | `PASS` |
| `git diff --check` | `PASS` |

## Deterministic evidence

- A worker event with the same raw identity is accepted once and does not
  allocate a second durable Echo `seq`.
- Durable sequence values remain contiguous across the accepted event stream.
- A terminal success is authoritative; a later cancellation becomes
  `task.terminal_ignored` with `visibility=debug` and does not change either
  the Agent task or its Workflow state.
- Repeated cancellation observes one completed outbox row and one remote
  cancel call; the stable `agent-cancel-*` operation key is reused.
- Agent task and Workflow terminal state remain `succeeded`/`succeeded` for the
  success-first trace and `cancelled`/`cancelled` for the cancel trace.

## Scope and limitations

- C did not modify `backend/app/**`, `desktop/electron/agent-runtime/**`,
  worker pool, manifest, transport, migrations, packaging, or installation
  files.
- The deterministic worker in this evidence is a test-owned trace producer;
  it is not a substitute for the A-owned production AgentTaskService to
  embedded Electron worker wiring. That production-path proof remains for the
  B10 integration owner after A/B deltas are integrated.
- The checkout contains concurrent uncommitted changes outside C's write set:
  `backend/app/agents/command_outbox.py` and
  `backend/app/agents/durable_state.py`, plus
  `backend/tests/unit/test_agent_durable_state.py`. C preserved them and did
  not include them in the focused command scope.
- C did not run full regression, provider smoke, packaging, signing,
  installation-state, or cross-platform verification.
