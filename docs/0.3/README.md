# EchoDesk 0.3 文档包

更新时间：2026-07-12 | 当前源码：`0.3.1`

状态：确定性门禁、live GLM contract、packaged smoke 与真实安装态 GLM + AgentOS workflow 已通过；跨平台 CI、公开签名、Release 与公网切流按各自证据单独确认。

## 1. 0.3 定位

EchoDesk 0.3 把原有“功能堆叠 + 局部状态”收束为本地优先、按 principal 隔离、可恢复的 workflow 系统：

```text
会议输入 -> 知识沉淀 -> 任务执行 -> 产物生成 -> 分享归档 -> 诊断恢复
```

Claude Code / AgentOS 保留 Full Access 主路径，但纳入 task、event、lease、cancel、timeout、retry、Artifact import 和恢复边界。Desktop UI 收束为 Session Navigation、Workbench、Inspector，统一字体、图标、状态、文案与响应式行为。

## 2. 文档索引

| 文档 | 用途 | 状态 |
|---|---|---|
| [`../../PRD.md`](../../PRD.md) | 用户、范围、功能需求与验收 | 当前产品定义 |
| [`../../METRICS.md`](../../METRICS.md) | 北极星、输入指标与护栏 | 当前指标定义 |
| [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) | 总体架构、事务边界和已知 P2 | 当前实现快照 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 0.3 模块与 UX 架构细化 | 当前实现快照 |
| [`WORKFLOWS.md`](WORKFLOWS.md) | 核心用户 workflow 与异常路径 | 设计与实现参考 |
| [`DATA_MODEL_AND_CONTRACTS.md`](DATA_MODEL_AND_CONTRACTS.md) | identity、DB、REST、WS、IPC 契约 | 当前实现快照 |
| [`TEST_PLAN.md`](TEST_PLAN.md) | 测试层次、命令与 release gates | 当前门禁 |
| [`DEV_PLAN.md`](DEV_PLAN.md) | 0.3 原始实施拆分 | 历史计划 |
| [`PRE_DEV_CHECKLIST.md`](PRE_DEV_CHECKLIST.md) | 开发前检查 | 历史准入 |
| [`UX_CLICK_VALIDATION_2026-07-10.md`](UX_CLICK_VALIDATION_2026-07-10.md) | 可见浏览器点击与缺陷修复证据 | 验收记录 |

## 3. 已落地架构

### Principal 与 public 隔离

- server-issued tenant、user(owner)、device、session 与 credential。
- Meeting、segments、RAG、Artifact、Workflow、Agent、WebSocket 和 storage 按 tenant / owner scope。
- session 支持 enroll、renew、claim、credential rotate、additional device 与 revoke。
- admission、quota、resource ticket 与 host-admin policy fail closed。

### Workflow Kernel

- durable run/event、idempotency、revision、deadline、cancel、retry 和 lineage。
- domain write、run/event 和 transactional outbox 使用同一个 SQLite Unit of Work。
- execution lease + fence + heartbeat 阻止多实例双执行。
- per-consumer、scope lane 和 global lease recovery 处理 outbox 失败与慢消费者。

### RAG

- ingest/delete/workspace scan/meeting projection 是 durable workflow。
- `/rag/ask` 是随连接取消的 SSE 读流，只有 `done` 终帧代表完成。
- SQLite manifest 与 revision 是跨进程 commit point，JSON index 是可重建 cache。
- Query、Ambient、Meeting 不再各自维护互相不可见的事实源。

### Meeting、Artifact 与 Agent

- 同 owner 单 active meeting；纪要清除使用 durable tombstone。
- Artifact metadata、link 和文件 staging 有统一提交边界。
- Agent raw event 持久化后再投影；bridge 支持 lease、backoff 和 failover。
- Agent Artifact 使用 bounded streaming import/proxy。
- terminal first-wins，晚到的冲突终态不覆盖已确认结果。

### Desktop / Android / TV

- Electron 默认 local-first，自包含 backend；public 模式必须显式设置 `ECHO_PUBLIC_DEMO=1`。
- renderer 使用 session-aware、可取消、带超时和结构化错误的 transport。
- WebSocket 收到 resync 后通过 REST 全量 rehydrate。
- Android / TV 使用设备身份桥接；公开资产必须由稳定 release 身份签名并校验。

## 4. UX 收口

- 左侧：实时记录、会议搜索和历史会议。
- 中间：转写 / 助手互斥切换，输入框 1–6 行自适应。
- 右侧：会议纪要 / 工作产物互斥切换，失败项就近重试。
- 全局使用同一套 Codex-like 系统字体和统一线性图标。
- 普通界面隐藏内部 ID、供应商实现、原始异常类名与低层 workflow 事件。
- 覆盖 411、960、1280、1920 viewport，长文本有明确换行或省略行为。

## 5. 最终门禁证据

| 门禁 | 结果 |
|---|---|
| Backend deterministic | `916 collected`；`18 live deselected`；`898 passed / 0 skipped`；coverage `87%`；pytest 自然退出 |
| Live GLM product contract | `2 / 2 passed` |
| Electron main-process contracts | `70 passed` |
| Desktop Playwright E2E | `95 passed` |
| Desktop scenarios | `29 passed` |
| Installed GLM + AgentOS full workflow | `1 / 1 passed` |
| Packaged local smoke | passed |

安装态完整 workflow 覆盖真实模型、Artifact 超时注入、退出重启、失败恢复、retry lineage、Agent 执行与 Artifact import、取消、超时及最终持久化恢复。packaged smoke 只验证打包边界，不替代该完整路径。

## 6. Agent 一致性状态

1. Agent terminal HTTP read 已加入 Workflow read barrier；关联 Workflow 未修复并核对为相同终态前，不返回领先终态。
2. migration 036 已加入 durable `agent_command_outbox`；`cancel_requested` 与 Agent/Workflow 状态、事件、outbox 同事务，远程取消由 fenced recovery worker 使用稳定 `Idempotency-Key` 执行与重放。

## 7. 发布边界

- 公开下载只以 GitHub Release 实际资产为准；CI artifact 和本机 `release/` 不等同于发布。
- Windows 在 Authenticode 未配置前拒绝 public publish。
- macOS ad-hoc 签名只用于本机验证，不等同于 Developer ID/notarization。
- Android / TV debug 或临时签名只用于开发；公开 APK 必须使用稳定 release 身份。
- public backend 切流需要隔离 smoke、客户端兼容、数据迁移与 rollback 验证。

## 8. 不做什么

- 不把 public backend 扩展为普通 principal 可调用的宿主机命令服务。
- 不在 0.3.1 引入团队、邀请、角色或跨 tenant 分享。
- 不把 Todo、Artifact 或 Agent 状态放回前端内存作为事实源。
- 不用 health probe、mock E2E 或单条 smoke 代替真实业务 contract。
- 不把已知 P2 写成已经解决。
