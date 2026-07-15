# F03 bundle analyst：native / optional / WASM / resource 报告

结论：`BUNDLE_ADAPTER_REQUIRED`；存在 `UNKNOWN_CRITICAL`，不能证明融合 kernel 可被当前 package layout 安全加载。

## 当前 Echo package lock

- `desktop/package-lock.json`：lockfile v3，实际 Electron `43.1.0`。
- lock 中 `optional: true` 条目数：`77`。
- native-like 扫描条目数：`34`；主要是 Electron/electron-builder、esbuild 的平台可选包和 node-gyp 工具链，不等同于生产 kernel native closure。
- 当前 checkout `desktop/node_modules` 不存在，因此没有安装态模块目录可供 ABI、`.node` 真实读取或 package exports 验证。
- 当前 tracked `desktop`/`backend` 文件中没有 `.node`、`.wasm` 或 `.bin` 文件。

## Claude source

Claude source `/Users/yoligehude/Downloads/src` 有 1902 个 TS/TSX/JS/JSX/CJS/MJS 文件，但没有发现 `package.json`、lockfile、tsconfig 或 bundler manifest。以下边界来自源码静态证据：

| 依赖面 | 证据 | 风险 |
|---|---|---|
| native addon | `src/services/voice.ts` 注释和 lazy loader 指向 `audio-capture.node` | 需要按 macOS arm64 / Windows x64 分包、ABI 检查和 `asarUnpack`/真实文件路径合同 |
| FFI | `src/upstreamproxy/upstreamproxy.ts` 使用 `bun:ffi` | Bun-specific API 不能直接视为 Electron Node API |
| child process | LSP、git、voice、terminal、hooks、worktree 等路径使用 `child_process`/`execFile`/`spawn` | kernel allowlist 明确禁止，必须排除或注入 Echo capability adapter |
| WASM | source 包含 WASM 语义和 `.wasm` 文件扩展 allowlist；未提供可哈希的资源 manifest | 不能确认资源是否需要 unpack、file URL 或 runtime extraction |
| optional imports | MCP、voice、LSP、bridge、plugin、remote 等路径含 feature/环境条件 | 必须建立 capability-scoped inclusion/exclusion matrix，不能依赖默认 tree-shaking |
| package exports | `QueryEngine.ts` 使用 `@anthropic-ai/sdk/resources/messages.mjs`、`lodash-es/last.js`、`@modelcontextprotocol/sdk` 等 | 版本和 export map 没有 lock 证据 |

## 当前 backend packaging 对比

`backend/packaging/echodesk-backend.spec` 使用 PyInstaller `collect_all`/`collect_data_files`/`collect_submodules`，并明确收集 Python native/data closure；Electron builder 再通过 `extraResources` 把该可执行文件放到 `resources/backend/`。这是现有 backend 的独立 package closure，不是 Claude kernel/worker closure，也不能替代 worker manifest。

现有 spec 还显式排除 `funasr`、`speech_recognition`、`nvidia`、`torch._dynamo`、`torch._inductor`、`triton`。这些排除规则不能自动推导 Claude source 的 native/optional 清单。

## 必须补齐的 package contract

1. 锁定 Claude source snapshot、SDK、optional dependency 和宏值，生成完整 dependency manifest。
2. 为每个平台生成 `agent-runtime/manifest.json`，对 worker、chunks、WASM、resource 和 native 文件做 SHA-256 绑定。
3. native addon 不得默认进入 `app.asar`；必须由平台资源策略明确 `asarUnpack` 或 `extraResources`，并用绝对资源路径加载。
4. dynamic `import()` 和 dynamic `require()` 必须有静态 inclusion list；未知 specifier 必须 fail closed。
5. package probe 必须在真实 Electron embedded runtime 与真实 macOS/Windows package layout 读回资源；当前 dry-run 未完成这两项。
