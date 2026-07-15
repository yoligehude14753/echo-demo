# F03 Cross-platform Probe Boundary Audit

日期：2026-07-15（Asia/Shanghai）
执行者：F03 Cross-platform probe owner
状态：`RUNTIME_BLOCKED`（F03 全量）；`CROSS_PLATFORM_BOUNDARY_PARTIAL`（本轮范围）

## 范围与硬边界

- 严格单线程执行，未派生 subagent；这是本轮用户约束对 F03 任务书“三个 subagent”条款的明确覆盖。
- 只新增 task-owned harness 与证据；未修改产品代码、构建/发布配置、Claude source、冻结合同或安装版。
- harness 只使用 Node built-ins，从 stdin 在 Sunny 执行；不上传文件，不读取 macOS `node_modules`、npm/Electron cache，也不共享这些目录。
- harness 不创建测试文件、不启动 EchoDesk、AgentOS、Claude CLI、localhost daemon 或正式安装包。
- macOS 不运行 Windows 分支；Windows 也不运行 macOS POSIX 分支。`drive`、`UNC`、`long path` 的结果按实际 host 分支记录，禁止 macOS 模拟 Windows。

## 版本与输入锚点

| 项目 | 证据 |
|---|---|
| 当前 checkout HEAD | `492053c53441793c220f3b8e1dd231f1faea6e42` |
| F03 任务书声明 baseline | `705c7392c6475bcb2036eee4636c6ee1b5ddb8cd` |
| F03 任务书只读 SHA-256 | `0e66d5b47395669b1db55dfd38377fe12a86c1c7130144dacf751168df8de563` |
| 当前 `desktop/package-lock.json` Electron | `43.1.0`（声明与 lock resolution 均为 `43.1.0`） |
| 本轮 harness SHA-256 | `90af163b334d87d1c4294bd90bb65743212be4fc7cc654ed93826e26656ea767` |

F03 任务书写的是 Electron 33 目标，而当前 checkout 的 lock resolution 是 Electron 43.1.0；这不是本轮通过/失败的推测，而是必须在 embedded-runtime probe 前解决的基线不一致。

## 原始命令与证据

macOS：

```text
node --check experiments/fusion-compatibility/F03/cross-platform/harness.cjs && node experiments/fusion-compatibility/F03/cross-platform/harness.cjs
```

Sunny：

```text
ssh -o BatchMode=yes -o ConnectTimeout=20 -o ConnectionAttempts=1 win-sunny-friend 'node -' < experiments/fusion-compatibility/F03/cross-platform/harness.cjs
```

- [macOS probe JSON](./macos-probe.json)
- [Sunny Windows probe JSON](./sunny-windows-probe.json)
- [同构 harness](../../../../../experiments/fusion-compatibility/F03/cross-platform/harness.cjs)

## 真实可证

### Sunny 调用性

- `ssh Sunny` 当前解析到主机名 `sunny`，DNS 失败，不能作为可调用入口。
- `ssh win-sunny-friend` 成功；只读 `cmd.exe /c ver` 返回 Windows `10.0.26200.8655`、主机 `SUNNY_FRIEND`、用户 `anyut`。
- 同一份 harness 通过 SSH stdin 在 Sunny 真实 Windows 上执行，退出码为 `0`；没有远端文件上传。

### macOS 与 Sunny 同构边界

| 检查 | macOS | Sunny Windows | 结论 |
|---|---|---|---|
| worker_threads load | Node 24.3.0，`passed` | Node 24.16.0，`passed` | shell Node 边界通过 |
| HOME isolation | `passed` | `passed` | 实际两端均收到了 synthetic home |
| PATH isolation | `passed` | `passed` | 实际两端均收到 scrubbed PATH |
| global Claude/Node hints | `0` 个残留变量 | `0` 个残留变量 | 环境过滤边界通过 |
| macOS 空格/中文路径 | 334 字符 shape `passed` | 不运行 | 仅 macOS shape 证据 |
| Windows drive path | 不运行 | 实际 Windows drive shape `passed`，root `C:\` | shape 证据 |
| Windows UNC path | 不运行 | 实际 Windows UNC shape `passed` | UNC filesystem 未证明 |
| long path | macOS 334 字符 shape `passed` | Windows 342 字符 shape `passed` | 两端均未做 filesystem access |
| node_modules/cache | 未读取 | 未读取 | builtin-only、stdin，同构且隔离 |
| 产品/daemon/network | 未启动/无网络动作 | 未启动/无网络动作 | 通过 harness 边界 |

## Blocked / 不可宣称

1. **本 cross-platform harness 的 shell boundary 与 embedded runtime 分层**：本目录的同构 stdin harness 是 shell Node boundary；独立 task-owned Electron probe 已在 macOS/Sunny 采集真实 Electron 43.1.0 main/worker fingerprint。两者不能互相替代，且 F03 文档目标 Electron 33 与当前 lock 的 43.1.0 仍需冻结口径。
2. **UNC filesystem blocked**：Sunny 没有提供 task-owned UNC share；本轮只验证了真实 Windows 上的 UNC path shape，未访问 `\\server\share`，也没有用 `\\localhost`、映射盘或 macOS 模拟替代。
3. **long-path filesystem blocked**：两端只在内存构造超长路径，没有创建文件或目录；因此不能宣称 NT path/Win32 API、manifest、实际 workspace 对 long path 的支持。
4. **安装态/包布局 blocked**：未构建 DMG/NSIS，未访问 Program Files、asar/unpacked resource、signed resources、ACL/UAC、junction/reparse point、child-process tree 或卸载流程；本轮不是 B14/B15 安装验收。
5. **HOME/PATH 只证明环境过滤边界**：两端 scrubbed child process 均通过，但因为 embedded Electron 未运行，不能把它升级为 bundled runtime 在无全局 Claude/Node 环境中的通过。
6. **F03 完成条件未满足**：cross-platform boundary 与 embedded runtime 最小 probe 已完成，但本轮仍未生成正式安装包、真实 asar/unpacked readback、UNC/long-path filesystem proof 或 Claude native closure，因此整体仍 blocked。

## 结论

本轮已获得真实 macOS 与真实 Sunny Windows 的同构 shell-Node boundary 证据：worker load、HOME/PATH isolation、依赖/cache 隔离，以及各自 host 原生 path shape 均可复核。Windows 证据不是 macOS 模拟，Sunny 通过真实 Windows SSH 执行。

但 F03 全量 verdict 必须保持 `RUNTIME_BLOCKED`：embedded Electron、UNC 实际 share、long-path 实际 filesystem、安装态/包布局与正式 B14/B15 仍未证明。下一步只能在具备目标 Electron runtime、task-owned Windows UNC/long-path filesystem fixture 和明确安装态权限后，继续同一 harness 的受限扩展；不得用本轮 shell Node 结果替代这些 gates。
