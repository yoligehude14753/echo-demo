# EchoDesk 0.3 文档包

日期：2026-07-10
状态：`0.3.0` 正式版收尾与本机验收
基线：`v0.2.50` / `e5574e9379e82f10057d5c84f401349c6f8e613b` [F-ECHO-001]  

## 0.3 定位

EchoDesk 0.3 的目标是把现有功能从“功能堆叠 + 局部补丁”收束为统一 workflow 系统。[F-ECHO-002]

核心主线：

```text
会议输入 -> 知识沉淀 -> 任务执行 -> 产物生成 -> 分享归档 -> 诊断恢复
```

Claude Code / AgentOS 接入不下线，正式纳入 `Agent Runner Workflow`。权限大不是问题；0.3 要解决的是状态追踪、事件 replay、取消、超时、重试、产物归档、历史恢复和测试门禁。

## 文档索引

| 文档 | 用途 | 进入开发前状态 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 0.3 治理后架构、模块边界、状态机 | 必读 |
| [WORKFLOWS.md](WORKFLOWS.md) | 核心用户 workflow、Happy/Sad/Boundary 路径 | 必读 |
| [DATA_MODEL_AND_CONTRACTS.md](DATA_MODEL_AND_CONTRACTS.md) | DB schema、REST、WS、IPC、事件契约 | 必读 |
| [TEST_PLAN.md](TEST_PLAN.md) | 单测、集成、E2E、contract、release smoke 门禁 | 必读 |
| [DEV_PLAN.md](DEV_PLAN.md) | PR 拆分、任务顺序、完成标准 | 必读 |
| [PRE_DEV_CHECKLIST.md](PRE_DEV_CHECKLIST.md) | 开发前检查项和当前准备状态 | 每次开工前检查 |

## 版本规则

- `v0.2.50` 是冻结基线，不再继续解释为新功能线。[F-ECHO-001]
- `0.3` 先做文档、架构、测试计划和 contract 准备，再进入实现。[F-ECHO-002]
- 开始代码实现时，版本从 `0.3.0-alpha.1` 起步。
- 任何来自旧实验线的能力必须先归入明确 workflow，再进入代码。

## 0.3.0-alpha.2 修复范围

- 历史会议列表首屏直接使用 `/meetings` 返回的 `n_segments` / `n_speakers`，不再等用户点击详情后才从 `0` 更新。
- 跨重启手动会议保留 24 小时续接窗口；超过上限仍为 `in_meeting` 时自动结束，避免顶栏累计数千分钟。
- backend pytest 和安装 smoke 全面隔离 `~/.echodesk`、真实 DB 与本机 `.env`，测试不再污染用户数据。
- 修复本机残留 `deploy-smoke` 会议：30 段转写与补生成纪要保留，错误运行态和结束时间已校正。

## 0.3.0 正式版收尾范围

- backend、desktop、Android / TV、安装资产和真实 E2E 统一使用无预发布后缀的 `0.3.0`。
- Desktop Pro 主模型统一为 `deepseek-v4-flash`，状态栏只展示模型名，不主动显示供应商文案。
- backend 安装流程加入 AgentOS / Claude Code runner 安装、LaunchAgent 托管、健康检查与卸载闭环。
- AgentOS 工作目录收口到 `~/.echodesk/agentos`，与 EchoDesk 用户数据目录一致，不再使用独立的 `~/.agentos`。

## 0.3.0-alpha.1 已落地范围

- Workflow Core：SQLite migration、`WorkflowService`、run/event replay、restore_unfinished、`/workflows/runs*` API、主 WS `workflow.event` / `workflow.snapshot`。
- Artifact：artifact metadata 和 `artifact_links` 入库，`/artifacts`、`GET /meetings/{id}/artifacts`、分享页和 outputs 清理改以 DB link 为事实源。
- Todo：artifact/todo 执行会创建 workflow run，前端从 workflow/todo 事件投影 running/failed/waiting_permission/done 状态。
- Agent：Claude Code / AgentOS 保留 full access 主路径，旧 Agent task DTO/API 兼容，同时写 workflow_events，并把 Agent 产物导入统一 artifacts。
- UI：outputs 面板统一恢复 artifacts/tasks，失败卡片接真实重试；Todo 行显示执行中、失败、等待授权等状态。
- Contract gates：REST route、WS event、IPC channel、script matrix、workflow HTTP scenario 均有测试覆盖。
- Desktop Pro：打包桌面端默认 local-first；public demo 由 `ECHO_PUBLIC_DEMO=1` 显式开启。

## 2026-07-10 验收记录

- 本机 backend 安装到 `~/.echodesk/source/backend`，独立 venv、PPT Node 依赖、import 和 health smoke 通过。
- `/Applications/EchoDesk.app` + `~/.echodesk/source/backend` 真实安装态 E2E 通过：Todo 首次超时失败、完整退出、失败态恢复、带 `retry_of` 重试成功、统一 artifact 入库/下载、真实 Claude Code Agent 产物导入、取消、超时及最终重启恢复。
- 安装包未设置 `ECHO_FORCE_LOCAL_BACKEND` 时仍确认 `isPublicDemo=false`；独立 smoke 从安装目录启动 backend 并在退出后释放端口。
- Backend 确定性门禁 `530 passed, 4 skipped, 20 deselected`，并验证测试前后真实 DB 会议数保持 `365`；desktop scenarios `25 passed`；typecheck、lint、version check、production build 全部通过。
- Yunwu `deepseek-v4-flash` 非流式与流式主模型请求均已真实通过；独立 fast gateway 的流式请求仍返回 HTTP 400，作为外部通道风险单列，不再用轻量健康探针代替业务调用结论。
- macOS DMG/ZIP 与 app 已完成 ad-hoc 签名和本机校验；正式公开分发仍需 Developer ID 签名与 notarization。

## 不做什么

- 不把 Claude Code 从产品里删除。
- 不因为权限大而弱化 Agent 能力。
- 不继续用前端内存维护会议产物归属。
- 不把 Todo 执行状态只藏在 `minutes_json` 里。
- 不新增第二套主 WebSocket。
- 不在 0.3 里继续用未登记的临时事件字段。

## 开发准入

进入开发前必须满足：

- FactStore health-check 通过。
- 本目录 6 份 0.3 文档存在。
- `README.md` 能指向本 0.3 文档包。
- `docs/GOVERNANCE_v0.2.50.md` 继续作为 0.2.50 基线说明存在。
- 开发 PR 必须能对应 [DEV_PLAN.md](DEV_PLAN.md) 中的一项。
