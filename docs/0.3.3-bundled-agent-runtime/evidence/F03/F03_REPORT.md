# F03 Electron Runtime & Packaging Compatibility Report

日期：2026-07-15（Asia/Shanghai）
任务：F03 Electron Runtime & Packaging Compatibility
状态：`RUNTIME_BLOCKED`；bundle 子结论：`BUNDLE_ADAPTER_REQUIRED`

## 1. 基线与提交

- Planned parent：`705c7392c6475bcb2036eee4636c6ee1b5ddb8cd`
- Effective compatibility baseline / Echo HEAD：`492053c53441793c220f3b8e1dd231f1faea6e42`
- Baseline delta：`fix(echo-033): align desktop e2e runtime contracts`
- Final commit：本报告与全部 F03 证据包含在本轮唯一 closeout commit；commit SHA 以最终 `git rev-parse HEAD` handoff 为准（报告不自嵌尚未生成的自引用 SHA）。
- Claude source：`/Users/yoligehude/Downloads/src`，只读。

## 2. 三个 subagent

主对话严格创建且仅创建以下 3 个 subagent，三者均未派生：

1. Electron runtime profiler — Poincare — `019f646a-c869-7751-a887-19fda65ddd22`
2. Bundle/package analyst — Hooke — `019f646a-c91b-78e3-b085-beac1effc9ba`
3. Cross-platform probe owner — Peirce — `019f646a-c9c6-7723-84fa-713ce36bb90e`

## 3. 原始验证命令

### macOS embedded runtime

```text
env -i HOME=<task-owned> PATH=/usr/bin:/bin F03_FIXTURE_DIR=<task-owned> \
  Electron.app/Contents/MacOS/Electron --no-sandbox --disable-gpu \
  --user-data-dir=<task-owned> electron-runtime-probe.cjs
```

### Sunny Windows embedded runtime

```text
scp electron-runtime-probe.cjs api-fixture.mjs js-fixture.js win-sunny-friend:<task-owned-temp>
ssh win-sunny-friend "C:\\Program Files\\nodejs\\node.exe node_modules\\electron\\cli.js \
  --no-sandbox --disable-gpu --user-data-dir=<task-owned> electron-runtime-probe.cjs"
```

### Cross-platform boundary harness

```text
node --check experiments/fusion-compatibility/F03/cross-platform/harness.cjs
node experiments/fusion-compatibility/F03/cross-platform/harness.cjs
ssh -o BatchMode=yes -o ConnectTimeout=20 win-sunny-friend \
  'node -' < experiments/fusion-compatibility/F03/cross-platform/harness.cjs
```

未运行正式 DMG/NSIS、未替换安装版本、未跑全量 desktop/backend/E2E，未启动 EchoDesk 产品、backend、AgentOS、Claude CLI 或 localhost daemon。

## 4. Runtime fingerprint

| 平台 | Electron | embedded Node | V8 | modules ABI | N-API | main/worker |
|---|---:|---:|---|---:|---:|---|
| macOS arm64 | 43.1.0 | 24.18.0 | 15.0.245.13-electron.0 | 148 | 10 | 同 PID，worker thread 1 |
| Sunny Windows x64 | 43.1.0 | 24.18.0 | 15.0.245.13-electron.0 | 148 | 10 | 同 PID，worker thread 1 |

两端真实 Electron main/worker 均通过 `process.versions`、V8、modules ABI、N-API、execPath、PID、`isMainThread` 与 `threadId` 检查；shell Node 仅作边界记录，未替代 Electron。

Electron API fixture 两端均通过：`fetch`、Web Streams、`AbortController`、`structuredClone`、`TextEncoder`、dynamic import、`.mjs` import、top-level await、`.js` require。

## 5. macOS / Sunny evidence

- [electron-runtime-fingerprint.json](./electron-runtime-fingerprint.json)
- [electron-runtime-fingerprint-macos.json](./electron-runtime-fingerprint-macos.json)
- [electron-runtime-fingerprint-sunny-windows.json](./electron-runtime-fingerprint-sunny-windows.json)
- [macos-probe.json](./macos-probe.json)
- [sunny-windows-probe.json](./sunny-windows-probe.json)
- [runtime-api-compatibility-matrix.md](./runtime-api-compatibility-matrix.md)
- [cross-platform/F03_REPORT.md](./cross-platform/F03_REPORT.md)
- [bundle-analyst/F03_REPORT.md](./bundle-analyst/F03_REPORT.md)

Sunny 的 probe 真实在 `win-sunny-friend` Windows x64 执行；只使用 Sunny 自有 task-owned `%TEMP%`、npm cache 和 Electron runtime，没有共享 macOS `node_modules`/cache。

## 6. Bundle/package evidence

- [bundle-module-graph.json](./bundle-module-graph.json)
- [native-and-optional-dependency-report.md](./native-and-optional-dependency-report.md)
- [packaged-layout-contract.md](./packaged-layout-contract.md)
- [bundle-analyst/bundle-module-graph.json](./bundle-analyst/bundle-module-graph.json)
- [bundle-analyst/native-and-optional-dependency-report.md](./bundle-analyst/native-and-optional-dependency-report.md)

当前 Echo package lock 实际为 Electron 43.1.0、Vite 7.3.6、TypeScript 5.9.3、electron-builder 26.15.3；F03 冻结文档仍写 Electron 33.4.x、Vite 6.0.5、TypeScript 5.7.2。当前 package layout 只有 backend extraResources，没有 agent worker/manifest/chunks/WASM/native hash contract。

Claude source 没有 package manifest/lock/tsconfig/bundler manifest；静态 closure 观察到 `@anthropic-ai/sdk/*.mjs`、`src/*.js` alias、dynamic import/require、`bun:bundle`、`bun:ffi`、child process、native addon、WASM/binary/resource 与 HOME/`.claude`/settings/plugins/memory 依赖。完整生产 import closure 仍为 `UNKNOWN_CRITICAL`。

## 7. Critical gaps

1. F03 文档目标 runtime 与当前 effective baseline lock resolution 不一致，必须由总控冻结版本口径。
2. Claude source 没有可验证的 package/lock/source manifest，SDK、optional dependency、宏值和 generated closure 未锁定。
3. native addon、`bun:ffi`、child process、WASM/binary、dynamic specifier 尚未映射到 Echo adapter 与平台 ABI/hash manifest。
4. 当前 package 没有 agent-runtime manifest/worker，未建立 asar/unpacked 真实 resource readback。
5. macOS 空格/中文与 Sunny drive/UNC/long-path 的 path shape 已有真实 host 证据，但 UNC share 与 long-path filesystem 实际读写未执行。
6. 未执行真实 Echo signed app、Windows Program Files/NSIS、签名资源、ACL/UAC、安装/卸载和 packaged worker load；这些不属于本轮 dry-run 可宣称范围。
7. 当前 Echo main/backend 仍有 HOME、source backend、PATH/python fallback 代码路径；不能将 task-owned isolation probe 升级为产品 bundled kernel isolation 通过。

## 8. Verdict

| Verdict | 结果 | 说明 |
|---|---|---|
| `RUNTIME_COMPATIBLE` | 不选 | 最小 Electron runtime/API probe 通过，但不覆盖 Claude closure/package gates |
| `BUNDLE_ADAPTER_REQUIRED` | 选中 | source alias/dynamic/native/optional/WASM/resource 与 manifest/asar policy 尚未完成 |
| `RUNTIME_BLOCKED` | 选中 | 完整 F03 仍有 unknown critical、真实 package readback 和安装态 gates 未证明 |

F03 不得向 F04 或生产 Batch 声称 runtime/package 已兼容；下一步必须先冻结 Electron 版本口径、锁定 source closure、生成 manifest/hash/ABI contract，再在真实 packaged layout 补跑受限 readback。
