# F01 Source / Version Compatibility Report

## 1. 结论

- F01 direct-vendoring verdict: `SOURCE_ADAPTER_REQUIRED`
- F04 recommendation from the F01 evidence: `F04_READY_WITH_ADAPTERS`
- Release/direct-import status: `SOURCE_BLOCKED`
- `claude_code_version` / `declared_version`: `unknown`，没有猜测

重新分类的关键是：F04 任务书要求的是 task-owned 的 adapted loop slice + fake model/tool deps，而不是把完整 `query.ts` production closure 直接 vendoring。当前 sha256 snapshot 足以绑定非生产实验输入；它不足以生成最终 release manifest，也不足以证明完整 production closure 可 direct import。

因此，`SOURCE_BLOCKED` 仅保留给完整 direct-vendoring / final-release provenance；不能把“不能 direct import”推导成“不能 fork/adapt 开始 F04”。

### 1.1 三层重新分类

#### A. `HARD_BLOCKS_F04`

基于现有证据，对“task-owned 最小 one-tool loop slice + fake deps”没有已证明的 F01 hard blocker。F04 可以把下列完整 production-path 缺失项排除在 slice 之外。

如果 F04 错误地选择直接运行完整 `query.ts`，则以下是精确的 hard missing set（不是 lexical unresolved 总数）：

- `types/message.js`
- `constants/querySource.js`
- `query/transitions.js`
- `services/compact/reactiveCompact.js`
- `services/contextCollapse/index.js`
- `services/skillSearch/prefetch.js`
- `jobs/classifier.js`
- `services/compact/snipCompact.js`
- `utils/taskSummary.js`

这组文件只阻断 literal full-query path；它们不阻断显式 fork/adapt 的 F04 slice。F04 仍必须把 slice 的实际文件清单、adapter 输入输出和 fake deps 写入自身 evidence，并保持无网络、无凭证、无生产路径副作用。

#### B. `F04_ADAPTER_WORK`

- 将 `bun:bundle` / `bun:ffi` / `feature(...)` 和 `MACRO.*` 固定为实验 adapter 的显式常量；不需要先还原 Claude release version。
- 将 `src/...` alias 改为实验目录的确定性相对 import，或在实验 bundler 中建立只读 alias。
- 不导入 `query/deps.ts` 的 `productionDeps()`；它会引入真实 `queryModelWithStreaming`、compaction 和 platform dependencies。F04 应定义最小 fake `model/tools/events/clock/ids` ports。
- 将 Anthropic message/tool blocks 映射到 Echo-shaped input、tool call/result correlation、terminal event 与 cancel 语义；SDK 类型只允许留在 adapter 边界。
- 从 slice cutting 出 fs、child_process、net/http/https、auth/config/session、HOME/PATH、native/UI/REPL/update 模块；这些是 adapter/exclude 任务，不是 snapshot identity blocker。
- 对上述 literal missing set 逐项做 `DIRECT / LOSSLESS_ADAPTER / SEMANTIC_REWRITE / UNSUPPORTED` 归属；one-tool happy path 可以只选择已闭合的最小字段集。

#### C. `RELEASE_ONLY_BLOCKERS`

- Claude upstream release/version/commit 与 package provenance 仍为 `unknown`。
- Claude package lock、Anthropic SDK exact version、native/optional ABI 版本不可证明。
- build macro producer/value 与 generated-file manifest 尚未绑定。
- 完整 production import closure 和最终 agent kernel manifest 尚未达到发布级 hash binding。

这些阻断最终发布 manifest、正式 packaged kernel 和 version compatibility certification；它们不阻断基于当前 sha256 snapshot 启动非生产 F04。

### 1.2 F04 最小 one-tool loop closure

当前证据可以界定 closure 的边界，但不能把 full-query lexical graph 当作 F04 closure：

| Slice 层 | F04 使用方式 | 是否进入最小运行 closure |
|---|---|---|
| Claude loop semantics | 以 `query.ts` 为只读参考，fork 需要的 loop 状态/transition 片段 | 是，按需复制到 task-owned experiment；不直接 import 整个文件 |
| Canonical message/tool/event shapes | adapter-owned 最小 schema | 是 |
| Model | fake deterministic model，产生一个 tool call 与 continuation | 是 |
| Tool | fake Echo-shaped tool registry/result | 是 |
| Production `query/deps.ts` | 不使用；其真实 model/compaction imports 被切掉 | 否 |
| Anthropic SDK | 不进入 fake path；只在 adapter 类型边界保留可验证字段 | 否 |
| fs/process/network/auth/native/UI | 明确排除 | 否 |
| literal missing set | 不选择 full `query.ts` path | 否 |

结论：当前 snapshot identity 足以安全启动 non-production F04，前提是 F04 自己先写出并 hash-bind 这个 bounded slice closure；若要求零 adapter、直接执行完整 `query.ts`，则应判 `HARD_BLOCKED_F04`。

## 2. 基线与身份

| 项目 | 值 | 证据 |
|---|---|---|
| Planned parent | `705c7392c6475bcb2036eee4636c6ee1b5ddb8cd` | `git show -s`；为 effective HEAD 的直接 parent |
| Effective compatibility baseline | `492053c53441793c220f3b8e1dd231f1faea6e42` | `git show -s`；branch ref 与 origin ref 同步 |
| Baseline delta | `fix(echo-033): align desktop e2e runtime contracts` | commit subject |
| Effective branch | `codex/echo-033-final-rc` | local/remote ref 指向 effective HEAD |
| Echo product version | `0.3.3` | `desktop/package.json:3` |
| Claude source root | `/Users/yoligehude/Downloads/src` | FUSION gate / task book |
| Claude source snapshot | `sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a` | canonical full regular-file manifest |
| Source manifest | `claude-source-manifest.sha256`，1,913 行 | byte-for-byte regenerated diff = no differences |
| Report commit marker | `c38d8be7d8e24c81c9e9530596a56cab594d9f5d` (pre-amend evidence commit; final SHA is the amended HEAD) | self-referential report field；最终权威 SHA 以 `git rev-parse HEAD` 为准 |

Canonical snapshot 规则：全部 1,913 个 regular files，包含现有 `.DS_Store`，按相对路径排序，以 SHA-256 file digest 形成标准 shasum 行，再对 manifest bytes 做 SHA-256。其他 subagent 使用了排除 `.DS_Store` 或不同分隔符的 digest；这些不是本报告的 canonical identity，已在 F01-G02 记录。

## 3. 三个 subagent 及结果

| Subagent | 结果 | 整合结论 |
|---|---|---|
| Provenance investigator | Echo target branch/base/tree provenance 可证实；Claude source 无 Git/package/version，建议 `BLOCKED_WITH_EVIDENCE` | 采纳；主报告以 effective HEAD `492053c` 为 Echo baseline |
| Import/dependency cartographer | 递归 closure 约 1,786 nodes、593 unique unresolved（其 probe）；约 15,284 import/require occurrences、约 925 行 `src/...` alias、196 处 `bun:bundle` | 与主探针方向一致；本报告 canonical graph 采用独立 lexical traversal 的 1,780 nodes / 11,988 edges / 616 unresolved counts，并明确 over-approximation |
| Compatibility auditor | Bun macro/alias、依赖版本、generated closure、kernel side effects 均未闭合；对 full/direct path 建议 `SOURCE_BLOCKED` / `FUSION_BLOCKED` | 采纳为 direct/release 结论；对 task-owned adapted slice 下调为 `F04_READY_WITH_ADAPTERS` |

三个 subagent 均只读、未修改 source/产品代码、未下载/升级依赖、未跑全量测试、未派生 subagent。

## 4. 关键证据

### 4.1 Echo baseline

- `git show -s --format='%H%n%P%n%s' 492053c...`：effective HEAD、唯一 parent、delta subject。
- `git rev-parse 492053c^{tree}`：target tree `c1ac0152e501b494ec147ef01d71c5b33f3220c8`。
- `desktop/package.json:3,9,82,90-91`：Echo `0.3.3`、Node `>=24.0.0`、Electron `43.1.0`、TypeScript `^5.5.3`、Vite `^7.3.1`。
- `desktop/package-lock.json`：lock-resolved Electron `43.1.0`、TypeScript `5.9.3`、Vite `7.3.6`、electron-builder `26.15.3`、esbuild `0.28.1`；Anthropic SDK absent。
- 当前现场 Node probe：`v24.3.0`；Electron binary unavailable，未作运行时放行。

### 4.2 Claude source provenance

- `find /Users/yoligehude/Downloads/src -type f`：1,913 regular files；extensions 为 TS 1,332、TSX 552、JS 18、`.DS_Store` 11。
- root 无 `.git`、package manifest、npm/pnpm/yarn/Bun lock、tsconfig 或 bundler manifest。
- `query.ts` SHA-256：`74e0ce0d86cfd453add8dc1d15ccb6311b02964b8321e3721b8e71fbd87252ce`。
- `QueryEngine.ts` SHA-256：`7df34a6a6d106927403d49e3405dfaf70da37cae5b644b658ebaf0b877988af6`。
- `utils/permissions/filesystem.ts:51` 只声明 `MACRO.VERSION`；`entrypoints/cli.tsx:37-40`、`commands/version.ts:6-8` 依赖 build-time macro。
- 无来源可把 `MACRO.VERSION` 解析成具体 Claude Code release/commit；按任务书必须保持 `unknown`。

### 4.3 query closure

- `query.ts:5`：`@anthropic-ai/sdk/resources/index.mjs`。
- `query.ts:105`：`bun:bundle` feature macro。
- `query.ts:16,19,67,70,116,119`：feature-gated dynamic/require candidates。
- lexical graph 见 `query-production-import-graph.json`；direct unresolved 包括 `types/message.js`、`constants/querySource.js`、`query/transitions.js`、`services/compact/reactiveCompact.js`、`services/contextCollapse/index.js`、`jobs/classifier.js` 等。
- source closure references fs、child_process、net/http/https、process/env、settings/auth/session、Anthropic/AWS/provider/MCP clients、ws/undici/axios，并有 native/optional references `audio-capture.node`、`image-processor-napi`、`color-diff-napi`、`modifiers-napi`、`sharp`。
- source 内 `.node` / `.wasm` binary count 为 0；package/ABI/version 仍未绑定。

### 4.4 Frozen contract comparison

`CONTRACT_FREEZE_V1.md` `4` 的 kernel import allowlist 排除 `node:fs`、`node:child_process`、`node:net`、`node:http`、`node:https`、`electron` 和 Claude auth/config/session/update/bridge modules。当前 full query closure 命中多项，因此 full/direct path 必须 REWRITE/EXCLUDE，不能 DIRECT；F04 adapted slice 应在边界外切掉这些 imports。

`CONTRACT_FREEZE_V1.md` `6-8` 要求 Echo authoritative model snapshot、Anthropic protocol adapter、revision/credential handle 和 canonical events；这些是 F04 adapter contract 与 release manifest 的约束，不是当前 snapshot 启动 fake-deps 实验的前置 version gate。

## 5. 命令记录

以下均为只读命令；未执行网络安装、依赖升级、生产编译、全量测试或 Claude binary launch。

`
python3 /Users/yoligehude/Desktop/all/_platforms/principles/factstore/scripts/health_check.py --project /Users/yoligehude/.codex/worktrees/2701/echo
git status --short --branch
git show -s --format='%H%n%P%n%s' 492053c53441793c220f3b8e1dd231f1faea6e42
git rev-parse 492053c53441793c220f3b8e1dd231f1faea6e42^{tree}
find /Users/yoligehude/Downloads/src -type f -print | wc -l
find /Users/yoligehude/Downloads/src -maxdepth 3 \( -name 'package.json' -o -name '*lock*' -o -name 'tsconfig*.json' \) -print
shasum -a 256 /Users/yoligehude/Downloads/src/query.ts
rg -n 'MACRO\.|bun:bundle|process\.env|require\(|import\(' /Users/yoligehude/Downloads/src/query.ts /Users/yoligehude/Downloads/src/query
`

Health-check 结果：当前 worktree 的 FactStore 有 36 facts；tentative/stale/conflict 为 0；5 条 high-volatility facts expired。没有使用这些过期事实作为 F01 结论依据。对用户指定的 `/Users/yoligehude/Desktop/all/echo` 主工作树直接运行时，events dir 缺失；本任务使用当前独立 worktree 的 absolute project path 重新执行并获得上述结果。

## 6. Evidence paths

- `docs/0.3.3-bundled-agent-runtime/evidence/F01/claude-source-identity.json`
- `docs/0.3.3-bundled-agent-runtime/evidence/F01/claude-source-manifest.sha256`
- `docs/0.3.3-bundled-agent-runtime/evidence/F01/query-production-import-graph.json`
- `docs/0.3.3-bundled-agent-runtime/evidence/F01/dependency-compatibility-matrix.md`
- `docs/0.3.3-bundled-agent-runtime/evidence/F01/source-gap-ledger.md`
- `docs/0.3.3-bundled-agent-runtime/evidence/F01/F01_REPORT.md`

## 7. Final verdict mapping

- `SOURCE_COMPATIBLE`: NO（不存在零适配 direct-import 证据）
- `SOURCE_ADAPTER_REQUIRED`: YES（F04 non-production slice 的正确分类）
- `SOURCE_BLOCKED`: YES，仅针对 full/direct vendoring、最终 release provenance 和正式 packaged kernel
- F01 experimental status: `accepted_with_adapters` / `SOURCE_ADAPTER_REQUIRED`
- F04 recommendation: `F04_READY_WITH_ADAPTERS`
- F04 不能借此跳过 F02/F03；该建议只表示 F01 不再是 adapted F04 的 hard blocker

## 8. Required next evidence

### F04 immediately required

- Task-owned adapted slice 的精确文件清单与 hash。
- fake model/tool/event/cancel loop 的 canonical trace。
- 所有 adapter 字段与 terminal/error/cancel 语义的明确归属。
- 证明 slice 不导入 forbidden fs/process/network/auth/session/native/UI 路径。

### Release re-review required

- Verifiable Claude source release/commit and exact dependency lock。
- Build macro values/producer and generated file manifest。
- Closed full-query production graph with every dynamic edge classified。
- Kernel-safe import graph proving no forbidden direct fs/process/network/auth/session/native dependency。
- Actual Echo Electron worker/runtime probe and packaged resource proof in F03。
