# F03 Bundle / Package Analyst 报告

日期：2026-07-15
角色：F03 bundle/package analyst
subagent：`0`（按用户明确要求严格不派生）
正式构建/安装：未执行
总体 verdict：`RUNTIME_BLOCKED`
bundle verdict：`BUNDLE_ADAPTER_REQUIRED`

## 1. 输入与基线

- 当前 checkout：`492053c53441793c220f3b8e1dd231f1faea6e42`，detached HEAD。
- F03 文档要求 baseline：`705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`；不匹配。
- F03 文档输入来自只读主 checkout：`/Users/yoligehude/Desktop/all/echo/docs/0.3.3-bundled-agent-runtime/`。
- Claude source：`/Users/yoligehude/Downloads/src`；观察到 1902 个代码文件，但没有 package manifest、lockfile、tsconfig 或 bundler manifest。
- 当前 `desktop/package-lock.json` 实际锁定：Electron `43.1.0`、Vite `7.3.6`、TypeScript `5.9.3`、electron-builder `26.15.3`；与 F03 文档期望的 Electron `33.4.x`、Vite `6.0.5`、TypeScript `5.7.2` 不一致。

## 2. 已执行的只读 dry-run

执行内容：

1. 读取 `desktop/package.json`、`desktop/package-lock.json`、`desktop/vite.config.ts`、`desktop/tsconfig.json`、Electron main/backend path code 和 PyInstaller spec。
2. 静态扫描 Echo 与 Claude source 的 ESM/CJS、`.mjs`、`.js` specifier、alias、dynamic import/require、child process、native/optional/WASM/resource 线索。
3. 用 stdin 注入的 Node 脚本在内存中模拟 `dev`、unpacked、asar、macOS 空格/中文路径、Windows drive/UNC 路径，以及 HOME/PATH 为空的 isolation 输入；没有创建构建产物。
4. 读取当前 host fingerprint：Darwin arm64；确认 `desktop/node_modules` 和 Electron binary 均不存在。

已执行：task-owned Electron 43.1.0 embedded main/worker/API probe（macOS 与 Sunny）；未执行正式 Vite/electron-builder build、DMG/NSIS、安装态 readback、native/WASM 实际加载。

## 3. 当前 layout 结论

当前 electron-builder `files` 只有 `dist/**`、`electron/**`（排除 tests）、`backend.config.json`、`package.json`；`extraResources` 只有 `resources/backend/echodesk-backend[.exe]`；没有 `agent-runtime` worker、manifest 或 `asarUnpack`。现有 backend 资源路径 `process.resourcesPath/backend/...` 可作为未来确定性路径模式，但不是 Claude kernel layout 证据。

建议的 future-only layout 已在 `packaged-layout-contract.md` 定义：`Resources/agent-runtime/{manifest.json,worker.mjs,chunks/**,resources/**,wasm/**,native/<platform>-<arch>/**}`。本轮没有修改发布配置。

## 4. 关键 module / resource 观察

- Claude `QueryEngine.ts` 同时使用 `bun:bundle`、`@anthropic-ai/sdk/...mjs`、`src/...js` alias、相对 `./...js` specifier、动态 `require()` 和 feature-gated closure。
- Claude source 的 voice/LSP/git/terminal/bridge/remote 路径引入 native addon、`bun:ffi`、`child_process`、HOME/`.claude`/settings/plugins/memory 依赖；这些不能直接进入 Echo kernel allowlist。
- 当前 Echo renderer 也有动态 chunk：mammoth browser bundle、exceljs、qrcode；这证明 renderer 的 Vite dynamic import 机制存在，但不能替代 worker closure 证明。
- current lock 有 77 个 optional entries；没有安装的 `node_modules`，因此没有实际 module exports、ABI 或 optional package readback。

## 5. Critical gaps

1. **基线/依赖漂移**：当前 checkout 和 F03 文档 baseline、Electron/Vite/TypeScript 版本不一致；任何 runtime 结论都不能回写为冻结 baseline 结论。
2. **Electron runtime 已最小证明但完整 package 未证明**：两端 embedded main/worker fingerprint 已采集；Claude source closure、产品 packaged worker 与 resource readback 仍未观测，shell Node 仍不可替代。
3. **package 未包含 kernel**：当前 files/extraResources 没有 worker、manifest、chunks、WASM/native resource；也没有 asar/unpacked contract。
4. **source closure 未锁定**：Claude source 没有 package manifest/lock；完整 QueryEngine production closure、SDK 版本、宏值、optional dependency 未知。
5. **native/optional/WASM 未绑定**：`audio-capture.node`、`bun:ffi`、child process 和可选路径均没有平台/ABI/hash/asar policy；存在 unknown critical dependency。
6. **HOME/PATH isolation 不成立**：当前 Echo main 仍有 `os.homedir()`、`~/.echodesk/source/backend` 和 PATH `python3` 候选；Claude source 有 `.claude`、settings/plugins/memory 和环境变量读取。embedded kernel 必须完全改为 manifest + Echo 注入依赖。
7. **跨平台证据缺失**：Windows drive/UNC/long path 只做了 host-side `path.win32` 模型；Sunny、Program Files/NSIS、签名资源和真实 readback 尚未执行。
8. **流程条件未满足**：三个 subagent 与两端 embedded probe 已完成，但完整 package/安装态 gates 仍未完成，因此不能声称 F03 完成或 `RUNTIME_COMPATIBLE`。

## 6. Evidence index

- `electron-runtime-fingerprint.json`：当前 package resolution、embedded runtime 缺失和 baseline mismatch。
- `runtime-api-compatibility-matrix.md`：ESM/.mjs/.js/alias/dynamic/native/WASM/API/HOME/PATH 矩阵。
- `bundle-module-graph.json`：Echo 当前入口、Claude `QueryEngine` import slice、动态和未知 closure。
- `native-and-optional-dependency-report.md`：optional/native/WASM/resource 盘点与必须补齐的 contract。
- `packaged-layout-contract.md`：当前 layout、建议 layout、dev/unpacked/asar、路径与 fail-closed 设计。
- `macos-probe.json`：真实 macOS host 的 shell boundary 与 embedded runtime 结果；完整 package 仍 blocked。
- `sunny-windows-probe.json`：真实 Sunny Windows host 的 shell boundary 与 embedded runtime 结果；完整 package 仍 blocked。

本目录只包含本轮 F03 bundle-analyst 证据；未修改产品代码、发布配置、Claude source 或 frozen contract。当前 worktree 中此前已有的其它 untracked F03 文件未触碰。
