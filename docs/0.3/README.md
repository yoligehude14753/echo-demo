# EchoDesk 0.3 文档包

日期：2026-07-09  
状态：开发前准备完成中  
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
