# F01 Dependency Compatibility Matrix

## 判定

- Echo effective compatibility baseline: `492053c53441793c220f3b8e1dd231f1faea6e42`
- Claude source snapshot: `sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a`
- `claude_code_version`: `unknown`
- Verdict: `SOURCE_BLOCKED`

## Matrix

| 维度 | Echo 证据 | Claude source 证据 | 分类 | 结论 |
|---|---|---|---|---|
| 产品版本 | `desktop/package.json:3` = `0.3.3` | 无 package/发布文件 | DIRECT（仅 Echo 侧） | Echo 版本可证实；Claude release version 不可证实 |
| Git provenance | HEAD `492053c...`；parent `705c739...`；target tree `c1ac0152...` | `/Users/yoligehude/Downloads/src/.git` absent | ADAPTER_REQUIRED / BLOCKER | 内容 snapshot 可绑定，但不能替代上游 commit/release |
| Node | `desktop/package.json:9` `>=24.0.0`；现场 `node v24.3.0` | `keybindings/defaultBindings.ts:21-25` 存在局部 `>=24.2.0` 分支 | ADAPTER_REQUIRED | 只有局部交集，未证明完整 source runtime |
| Electron | `desktop/package.json:82` exact `43.1.0` | source 没有 Electron contract | ADAPTER_REQUIRED | 需 worker/runtime probe；F01 不放行 |
| TypeScript | declaration `^5.5.3`，lock `5.9.3` | 无 tsconfig/version | BLOCKER | module target、生成器和 compiler version 未绑定 |
| Vite/Bundler | declaration `^7.3.1`，lock `7.3.6` | `bun:bundle` / `bun:ffi`、`MACRO.*` | ADAPTER_REQUIRED | 需要 feature/macro adapter 与 build rewrite |
| Anthropic SDK | Echo `desktop/package.json` 无 `@anthropic-ai/sdk` | `query.ts:5` 及递归 closure 大量 SDK imports | BLOCKER | package/version/lock 不可证明，不能安装或猜测 |
| Internal alias | Echo Vite 仅有自身 `@` alias | 约 925 行 `src/...` alias，约 300 个文件 | ADAPTER_REQUIRED | 必须固定 alias root 或 rewrite |
| Local closure | Echo 无 Claude source closure | lexical traversal: 约 1,780 nodes、11,988 edges、616 unresolved refs | BLOCKER | 缺失项含 generated/feature-gated modules，不能闭合 |
| Dynamic loading | Echo 侧未提供 Claude loader contract | `query.ts` feature-gated `require/import`；全树约 602 dynamic/require edges | ADAPTER_REQUIRED | 必须为每个分支给出 DIRECT/ADAPT/REWRITE/EXCLUDE |
| Compile macros | Echo 无 Claude macro producer | `utils/permissions/filesystem.ts:51` 仅声明 `MACRO.VERSION`；`entrypoints/cli.tsx:37-40` 依赖 build-time inline | BLOCKER | 当前 snapshot 无法还原版本和宏值 |
| Filesystem | Echo frozen kernel allowlist 排除 fs | closure 使用 fs、settings、session storage | REWRITE / EXCLUDE | 只能通过 Echo capability adapter，kernel 不得直接依赖 |
| Process/commands | frozen contract 排除 child_process | closure 使用 child_process、shell、spawn/exec | REWRITE / EXCLUDE | 不得把 CLI/外部命令语义带入 kernel |
| Network | frozen contract 排除 net/http/https | closure 使用 Anthropic client、axios、ws、undici、MCP clients | REWRITE / EXCLUDE | 统一接入 EchoModelPort/受控 capability |
| Auth/config/session | frozen contract 禁止 Claude auth/config/session modules | `utils/auth.ts`、settings、`~/.claude/.credentials.json`、env fallback | REWRITE / BLOCKER | 必须移除外部事实源并映射 Echo snapshot |
| Native/optional | Echo lock 有自身依赖但无 Claude native packages | `audio-capture.node`、`image-processor-napi`、`color-diff-napi`、`modifiers-napi`、`sharp` references；source 内无 `.node` binary | EXCLUDE / BLOCKER | ABI、platform package、optional install source 未知 |
| Generated files | Echo toolchain 可锁定 | source 无 generation manifest/命令；direct closure 缺失 `types/message.js`、`constants/querySource.js`、`query/transitions.js` 等 | BLOCKER | 不能把 generated output 当作已存在 |
| CLI shape | Echo existing minutes-kit invokes external Claude CLI with stream-json flags | `main.tsx:976` 支持相关 flags；`entrypoints/cli.tsx:37-40` 支持 version | DIRECT（仅外部 CLI 边界） | 这是 source-shape 兼容证据，不是 source vendoring 放行 |

## 允许的后续路线

1. 先补齐可验证 Claude release/commit、package lock、build macro provenance 和 generated-file manifest。
2. 将 Claude source 拆成 kernel-safe closure；fs/child_process/network/auth/config/session/native/UI/REPL 模块必须排除或经明确 adapter。
3. 在 F02/F03/F04 重新证明 canonical contract、Electron worker 和 packaged load 后，才能重评 F01。

