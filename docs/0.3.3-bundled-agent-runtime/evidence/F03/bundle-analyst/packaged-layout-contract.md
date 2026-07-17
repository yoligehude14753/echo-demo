# F03 bundle analyst：packaged layout contract

状态：`BUNDLE_ADAPTER_REQUIRED`；本文是 dry-run 设计合同，不是已落地的发布配置。

## 当前 checkout 的实际 layout

`desktop/package.json` 当前 builder 配置：

```text
app.asar (默认 builder app payload)
  dist/**
  electron/**
  backend.config.json
  package.json

Resources/  (extraResources，位于 asar 外)
  backend/echodesk-backend       # macOS/Linux
  backend/echodesk-backend.exe   # Windows
```

当前没有：

- `agent-runtime/worker.mjs`；
- kernel/chunk/resource manifest；
- `asarUnpack` 规则；
- native/WASM 资源归属；
- worker thread entry 或 worker manifest hash；
- package layout 的 Windows drive/UNC 真实读回证据。

Electron main 对现有 backend 使用 `process.resourcesPath/backend/<platform executable>`，并在 packaged mode 缺失时拒绝 source backend fallback。这条 backend 资源路径是可复用的确定性模式，但当前 `resolveBackendCwd()` / `pythonCandidates()` 仍保留 `ECHO_BACKEND_CWD`、`~/.echodesk/source/backend`、仓库 backend、`/usr/bin/python3` 和 PATH `python3` 候选，不能作为 embedded kernel isolation 合同。

## 建议的 kernel layout（仅设计）

```text
Resources/
  agent-runtime/
    manifest.json
    worker.mjs
    chunks/**
    resources/**
    wasm/**
    native/<platform>-<arch>/**       # 必须在 asar 外或明确 unpack
```

manifest 至少绑定：`schema_version`、Echo baseline SHA、Claude source snapshot、Electron major、platform、arch、worker entry、chunk list、resource list、native list、SHA-256、load mode 和 excluded dependency list。worker 只能从 manifest 解析绝对资源路径；不读取 `process.cwd()`、`HOME`、PATH 搜索结果或用户 `.claude`。

## 三种运行模式

| 模式 | worker 根 | 允许行为 | 当前状态 |
|---|---|---|---|
| dev | repo/desktop 或显式 task-owned fixture | 可使用显式 fixture 路径；不得把 dev fallback 当 packaged proof | path-only dry-run 通过 |
| unpacked | `process.resourcesPath/agent-runtime` | 读取 manifest、校验 hash、加载 worker | 当前未包含 |
| asar | `process.resourcesPath/agent-runtime` 中的 JS/JSON chunk；native/WASM 依赖按 manifest 明确外置/解包 | 不允许把虚拟 asar cwd 当 native spawn cwd | 当前无 agent layout/asarUnpack |

## 路径 dry-run

已用内存中的 `node:path`/file-URL 模型覆盖：

- macOS `/Applications/Echo Desk.app/Contents/Resources`；
- POSIX 含空格和中文的 dev 路径；
- Windows `C:\\Program Files\\Echo Desk\\中文\\resources`；
- Windows UNC `\\\\server\\share\\Echo Desk\\resources`。

上述模型均生成稳定的 `agent-runtime/worker.mjs` 绝对路径和 file URL。Windows 结果是 host-side `path.win32` 模型，不是 Sunny Windows runtime/readback 证明；长路径策略、NSIS 实际安装目录、签名资源和 UNC 访问仍为 `BLOCKED`。

## fail-closed 规则

1. manifest 缺失、hash 不符、platform/arch 不符或 entry 不在 allowlist：拒绝启动。
2. dynamic import/require 的 specifier 无法映射到 manifest：拒绝启动，不回退到 PATH/HOME。
3. native addon 在 asar 虚拟路径中不可加载或 ABI 不符：拒绝该 capability，不静默回退。
4. HOME/PATH/`CLAUDE_CONFIG_DIR`/global Claude 缺失时，worker 仍必须只依赖 manifest 和 Echo 注入 deps；否则判 `RUNTIME_BLOCKED`。
5. 只做 dry-run，不能把当前 package config 或源码扫描升级为 installed/package acceptance。
