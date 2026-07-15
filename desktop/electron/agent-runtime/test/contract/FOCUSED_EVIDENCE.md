# B04K focused-verification evidence

- Role: `focused-verification` (C)
- Input base / current HEAD: `1904cb8c49502d64c53ff163d6e04b88d396c751`
- Compatibility baseline: `492053c53441793c220f3b8e1dd231f1faea6e42`
- F04 evidence: `db57ddefc95c494c3785659db89befe6d8cf9c94`
- Contract: v1; no contract/version change
- Scope verdict: `BLOCKED` (not a contract-change request; `ACCEPTED_CANDIDATE` is not justified)

## Focused commands

| Command | Result |
|---|---|
| `node --experimental-strip-types --test desktop/agent-kernel/test/golden/contract-vectors.test.mjs` | PASS, 6/6; compact/budget vectors, forbidden kernel imports, corrupt checkpoint, manifest mismatch |
| `npm run typecheck` in `desktop/agent-kernel` | BLOCKED: `sh: tsc: command not found` |
| `npm run typecheck` in `desktop` | BLOCKED: `sh: tsc: command not found` |
| `node --experimental-strip-types --test desktop/electron/agent-runtime/test/contract/production-worker.contract.test.mjs` | BLOCKED before tests: Node `v24.3.0` strip-only loader rejects parameter property in `pool/worker-manager.ts:127` (`ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX`) |
| `node --check` on both C `.mjs` scripts | PASS |

## Evidence covered

- Contract vectors are pinned to v1, the compatibility baseline, F04 evidence, and B04K input base.
- `microCompactMessages` and `checkTokenBudget` are called by the final golden suite.
- Kernel forbidden-import scan passes.
- Corrupt checkpoint rejects with `CHECKPOINT_CORRUPT`.
- Build manifest mismatch rejects with `RUNTIME_BUILD_MISMATCH`.
- Production worker factory is task-owned and targets the real `WorkerManager`/`worker-entry` boundary, but the proof did not execute because the installed Node loader cannot load the current TypeScript implementation.
- The current kernel checkpoint code still emits `compactState.strategy: "none"`; no `agent.compaction.completed` or `agent.summary.updated` production path is present in the checked source. Therefore compact→summary→checkpoint and same-PID production proof remain open.

## Source-gap ledger inherited

`EXCLUDED_SOURCE_GAP`: `services/compact/reactiveCompact.js`, `services/contextCollapse/index.js`, `services/compact/snipCompact.js`, `utils/taskSummary.js`.

## C-owned changed files and hashes

```text
9c351c87581e5659af8b98c5f061b550b0fcb48e7acd3288fa38422fa16ca111  desktop/agent-kernel/test/golden/contract-vectors.json
a093a455a28e1f7b4ff8d248af258e367aa4fedc115d9fd66b29bb57634a5293  desktop/agent-kernel/test/golden/contract-vectors.test.mjs
5f4cc941ef35a357ae740f3866e4a5b0b25ae320bfc90cda8ecce08bd9083356  desktop/electron/agent-runtime/test/contract/worker-contract.json
b9df98c08fa17e469ab21d508562868de15db5ef446a9eea3d1e2e5a7a704a2d  desktop/electron/agent-runtime/test/contract/production-worker.contract.test.mjs
2fc7a290f2bb7055d16c8d2ac3015771531ca78a48209f4eb9a4b062e517e4a6  desktop/electron/agent-runtime/test/contract/production-worker-factory.mjs
```

## Scope audit

C changed only `desktop/agent-kernel/test/golden/**` and `desktop/electron/agent-runtime/test/contract/**`. No A/B implementation file, owner test, docs, package manifest, source manifest, F04 evidence, commit, push, PR, release, or independent B07 was created or modified by C.

## Remaining blockers

1. The focused kernel and desktop typechecks cannot run because `tsc` is absent in the current checkout.
2. The production worker proof cannot load `worker-manager.ts` under the installed Node `v24.3.0` strip-only loader because of a parameter property.
3. Even after loader/toolchain repair, the current kernel path must provide production compact and summary events plus a checkpoint with `microcompact` state before B04K can be accepted.
