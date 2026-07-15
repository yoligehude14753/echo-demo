# F03 bundle analyst：runtime / module compatibility matrix

状态：最小 embedded runtime/API probe `PASS`；完整 package closure 仍 `RUNTIME_BLOCKED`（未用 shell Node 替代）

## 证据边界

- checkout：`492053c53441793c220f3b8e1dd231f1faea6e42`，detached HEAD。
- F03 文档冻结 baseline：`705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`；当前 checkout 不匹配。
- `desktop/package-lock.json` 实际 resolution：Electron `43.1.0`、Vite `7.3.6`、TypeScript `5.9.3`、electron-builder `26.15.3`。
- F03 文档期望：Electron `33.4.x`、Vite `6.0.5`、TypeScript `5.7.2`。
- macOS/Sunny task-owned Electron probe 已采集 Node/V8/modules ABI；shell Node `v24.3.0`/`v24.16.0` 仅作为边界记录，不能替代 Electron。

## 矩阵

| 能力/形态 | 当前 Echo 代码证据 | dry-run 结果 | 可放行性 |
|---|---|---|---|
| Electron `process.versions` | task-owned Electron 43.1.0 main | macOS/Sunny 均已观测 | `PASS` |
| `worker_threads` / worker 内 fingerprint | task-owned Electron main worker | 同 PID，ABI 一致 | `PASS` |
| ESM | `desktop/package.json` 为 `type: module`；Vite/TS 使用 `ESNext` | 语法层存在 | 不能替代 Electron runtime proof |
| `.mjs` SDK specifier | Claude `QueryEngine.ts:2` 使用 `@anthropic-ai/sdk/resources/messages.mjs` | 依赖未锁定、未加载 | `BLOCKED` |
| `.js` TypeScript specifier | Claude `QueryEngine.ts` 大量 `./*.js` 与 `src/*.js` | 需要 source-aware bundler/alias contract | `ADAPTER_REQUIRED` |
| `src/...` path alias | Claude `QueryEngine.ts:6-13`、`QueryEngine.ts:72` 等；当前 Echo alias 只有 `@/*` | Claude alias 未配置 | `ADAPTER_REQUIRED` |
| JSX/TSX | Claude source 含 `tsx`；当前 Echo 使用 React TSX | 目标语言形态可表达 | 仍需真实 worker bundle proof |
| top-level await | task-owned `.mjs` fixture | macOS/Sunny 均通过 | `PASS` |
| dynamic `import()` | 当前 Echo `ArtifactPreviewModal.tsx` 动态加载 mammoth/exceljs，`MeetingShareModal.tsx` 动态加载 qrcode；Claude source 大量动态 import | 当前 Vite route 仅是 renderer chunk 证据 | kernel worker closure 未定 |
| dynamic `require()` / feature macro | Claude `QueryEngine.ts` 对 `MessageSelector`、`COORDINATOR_MODE`、`HISTORY_SNIP` 使用条件 require；依赖 `bun:bundle` feature | 未建立宏替换/树摇 contract | `ADAPTER_REQUIRED` |
| `fetch` / Web Streams / AbortController / structured clone / TextEncoder | task-owned API fixture | macOS/Sunny 均通过 | `PASS` |
| native addon | Claude source `services/voice.ts` 延迟加载 `audio-capture.node`；`upstreamproxy.ts` 出现 `bun:ffi` | 未建立 ABI/asar-unpack 归属 | `CRITICAL_GAP` |
| optional dependency | current desktop lock 中有 77 个 `optional` entries，含平台性 esbuild/Electron entries | 只完成 lock scan | 需 capability-scoped allowlist |
| WASM / binary resource | 当前 tracked `desktop`/`backend` 无 `.node/.wasm/.bin` 文件；Claude source 仍包含 `.wasm` 能力/资源语义与 binary extension allowlist | 未构建/未读取真实资源 | `BLOCKED` |
| child process / PATH | current Electron main 仍使用 `node:child_process` 启动 backend/Python；Claude source 大量 `child_process` | 可见外部进程路径 | 不满足 embedded kernel isolation |
| HOME / Claude config | current main 读取 `os.homedir()` 并检查 `~/.echodesk/source/backend`；Claude source 读取 `~/.claude`/settings/plugins/memory 路径 | 代码级依赖存在 | `CRITICAL_GAP` |

## 结论

在没有 exact F03 baseline、Electron embedded fingerprint、source lock/manifest、worker closure 和平台 probe 的前提下，不能给出 `RUNTIME_COMPATIBLE`。当前 bundle-only 结论为：`RUNTIME_BLOCKED`，并且即使 runtime binary 补齐，Claude source 仍至少需要 path alias、dynamic import/require、native/optional resource 和 HOME/PATH 边界适配。
