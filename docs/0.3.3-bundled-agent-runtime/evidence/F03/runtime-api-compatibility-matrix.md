# F03 Runtime API Compatibility Matrix

基线：有效 compatibility baseline `492053c53441793c220f3b8e1dd231f1faea6e42`；计划 parent `705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`。

## 实测 Electron embedded runtime

macOS arm64 与 Sunny Windows x64 使用同一 `electron-runtime-probe.cjs`，均为真实 Electron main + `worker_threads`，不是 shell Node 替代。两端均为 Electron 43.1.0 / Node 24.18.0 / V8 15.0.245.13-electron.0 / modules ABI 148 / N-API 10。

| 能力 | macOS arm64 | Sunny Windows x64 | 证据 | 结论 |
|---|---|---|---|---|
| `process.versions` / Electron identity | PASS | PASS | `electron-runtime-fingerprint-*.json` | runtime fingerprint 可复现 |
| main → `worker_threads` | PASS；同 PID，`threadId=1` | PASS；同 PID，`threadId=1` | 同上 | 满足同进程 worker 形态 |
| Node/V8/modules/N-API ABI 一致 | PASS | PASS | 同上 | 两端 main/worker 一致 |
| `fetch` | PASS | PASS | task-owned API fixture probe | Electron runtime 提供 |
| Web Streams (`ReadableStream`/`TransformStream`) | PASS | PASS | task-owned API fixture probe | Electron runtime 提供 |
| `AbortController` | PASS | PASS | task-owned API fixture probe | Electron runtime 提供 |
| `structuredClone` / `TextEncoder` | PASS | PASS | task-owned API fixture probe | Electron runtime 提供 |
| dynamic import | PASS | PASS | data URL + file fixture | runtime 语义可用 |
| `.mjs` import + top-level await | PASS | PASS | `api-fixture.mjs` | runtime 语义可用 |
| `.js` require | PASS | PASS | `js-fixture.js` | CJS fixture 可加载 |
| Claude `@anthropic-ai/sdk/*.mjs` closure | NOT RUN | NOT RUN | source 无 package/lock | 需 bundle adapter 与锁定依赖 |
| Claude `src/*.js` alias / macro `require` | NOT RUN | NOT RUN | 静态 graph only | 需 source-aware inclusion contract |
| native addon / WASM / optional package readback | NOT RUN | NOT RUN | 无 Claude install closure | `UNKNOWN_CRITICAL`，不得放行 |

## 环境隔离

- macOS：task-owned `HOME`、`PATH=/usr/bin:/bin`、独立 user-data-dir；未启动 EchoDesk/backend。
- Sunny：task-owned `%TEMP%\\echodesk-f03-electron-43`、scrubbed `USERPROFILE/PATH`、独立 user-data-dir；未共享 macOS node_modules/cache。
- Shell Node 24.3.0（macOS）与 24.16.0（Sunny）只作为边界记录，未作为 Electron 结论来源。

## 结论

Electron runtime 本身的 main/worker/API 最小探针在两端通过；这不能升级为完整 F03 通过，因为 Claude source import closure、native/optional/WASM/resource manifest、asar/unpacked readback、UNC/long-path filesystem 与真实 Echo package layout 仍未证明。综合 verdict 仍为 `RUNTIME_BLOCKED`，bundle 子 verdict 为 `BUNDLE_ADAPTER_REQUIRED`。
