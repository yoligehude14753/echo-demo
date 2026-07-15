# F01 Source Gap Ledger

| ID | Gap | Evidence | Classification | State | Closure required |
|---|---|---|---|---|---|
| F01-G01 | Claude release/version/commit unknown | source root has no .git/package/lock; `claude-source-identity.json` | BLOCKER | OPEN | Obtain verifiable upstream release or commit; otherwise keep `unknown` |
| F01-G02 | Snapshot identity encoding conflict across probes | provenance agent used source-only or alternate line encoding; canonical full manifest is `b1f141a...` and byte-diff verified | GOVERNANCE | RESOLVED_FOR_F01 | Use only `claude-source-manifest.sha256` canonical identity; do not mix alternate digests |
| F01-G03 | Build macro producer absent | `utils/permissions/filesystem.ts:51` declares `MACRO.VERSION`; no build manifest | BLOCKER | OPEN | Freeze all macro values and producer, or replace with adapter-owned constants |
| F01-G04 | Bun-specific compiler contract | `query.ts:105` imports `bun:bundle`; source also references `bun:ffi` | ADAPTER_REQUIRED | OPEN | Add approved macro/feature adapter or exclude affected paths |
| F01-G05 | Internal alias cannot resolve in Echo bundler | about 925 `src/...` alias lines across about 300 files; Echo Vite has no Claude alias root | ADAPTER_REQUIRED | OPEN | Add deterministic alias mapping or rewrite imports; add golden resolution fixture |
| F01-G06 | query production closure not closed | lexical graph reports 1,780 resolved nodes, 11,988 edges, 616 unresolved local references | BLOCKER | OPEN | Produce generated-file manifest and classify every unresolved edge |
| F01-G07 | Feature-gated/generated local modules absent | `reactiveCompact.js`, `contextCollapse/index.js`, `types/message.js`, `query/transitions.js`, `jobs/classifier.js`, `snipCompact.js`, `taskSummary.js` unresolved from `query.ts` | BLOCKER | OPEN | Prove generated inputs/outputs and fixed feature defines, or exclude |
| F01-G08 | SDK dependency/version/lock unknown | direct `@anthropic-ai/sdk/resources/index.mjs` plus recursive SDK imports; source no package manifest | BLOCKER | OPEN | Supply immutable package lock and exact SDK version; no install in F01 |
| F01-G09 | External dependency closure broad and unversioned | lexical closure includes axios, ws, undici, MCP, AWS/Vertex/Foundry, OpenTelemetry, zod, sharp and others | BLOCKER | OPEN | Split kernel-safe dependency set and bind versions |
| F01-G10 | Filesystem side effects in kernel candidate | closure uses fs, session storage, settings and file persistence | REWRITE / EXCLUDE | OPEN | Route through Echo capability tools; kernel import allowlist must pass |
| F01-G11 | Process/CLI side effects in kernel candidate | closure uses child_process, shell, spawn/exec and external CLI helpers | REWRITE / EXCLUDE | OPEN | Exclude from kernel or map to EchoTool with grant enforcement |
| F01-G12 | Network clients in kernel candidate | closure uses Anthropic API, axios, ws, undici, MCP and provider SDKs | REWRITE / EXCLUDE | OPEN | Map model calls to EchoModelPort; no direct network imports |
| F01-G13 | Claude auth/settings/session facts leak into runtime boundary | `utils/auth.ts` and settings paths use env, settings and `~/.claude/.credentials.json` | REWRITE / BLOCKER | OPEN | Use Echo ModelRuntimeSnapshot/GrantSnapshot; remove HOME/PATH/auth fallback |
| F01-G14 | Native/optional ABI not bound | references to `audio-capture.node`, `image-processor-napi`, `color-diff-napi`, `modifiers-napi`, `sharp`; no binary or lock | EXCLUDE / BLOCKER | OPEN | Exclude from kernel or provide per-platform immutable ABI evidence |
| F01-G15 | Existing gate toolchain statements stale | gate text states Electron ^33.4.0, TypeScript ^5.7.2, Vite ^6.0.5; effective baseline package is Electron 43.1.0, TS ^5.5.3, Vite ^7.3.1 | GOVERNANCE | OPEN | Re-freeze actual effective baseline before F02/F03 |
| F01-G16 | Runtime validation not performed | no production build, full tests, Claude binary launch, or dependency install per task scope | BLOCKER | OPEN | F03/F04 must provide runtime/packaged evidence |
| F01-G17 | Source-shape CLI compatibility is not vendoring compatibility | external CLI flags/stream-json shape align statically, but source import closure does not | BOUNDARY | RESOLVED | Keep external CLI adapter as separate path; do not use it as F01 source-compat proof |

## Fail-closed rule

Any OPEN item marked BLOCKER prevents `SOURCE_COMPATIBLE` and prevents F04 from treating F01 as accepted. No item is silently promoted from `unknown` to a version or runtime guarantee.

