# F04 deterministic adapter

Task-owned TypeScript adapter for the F02 message/model/tool/event/cancel boundary.
It has no Echo production imports, no Claude source imports, no network access, no
credentials, no AgentTaskService, and no platform side effects.

The adapter enforces:

- schema version `1` on input, model snapshot, grant snapshot, model events, and output events;
- exact source snapshot, manifest, Echo baseline, and runtime fingerprint matching before session open;
- stable task/operation/request identity and contiguous deterministic event sequence;
- `toolUseId`-only tool correlation, JSON-object tool arguments, and fail-closed unknown/duplicate results;
- typed cancel terminal events with first-terminal-wins and idempotent cancel calls.

## Deterministic commands

Run from the Echo repository root:

```sh
node --experimental-strip-types --check experiments/fusion-compatibility/F04/adapter/adapter.ts
node --experimental-strip-types --test experiments/fusion-compatibility/F04/adapter/test.mjs
```

The test uses only deterministic fake model/tool ports. The F03 macOS/Sunny
Electron worker probes and any loop-slice integration remain outside this
adapter-owned directory and are not run here.
