# EchoDesk v0.3.1 产品需求文档

版本：0.3.1
状态：实现与验收收口
日期：2026-07-12

## 1. 产品定义

EchoDesk 是一个本地优先的会议与办公数字分身工作台。它把会议采集、知识检索、纪要、Todo、办公产物和 Agent 长任务连接为可恢复、可追溯的 workflow，同时为 public demo 提供服务端身份与资源隔离。

一句话价值：**把一次会议或一组资料，从输入持续推进到可复用知识、可执行任务和可交付产物。**

## 2. 北极星指标

北极星指标为“已授权 Workflow 有效闭环率”，定义和目标见 [`METRICS.md`](METRICS.md)。

- 当前生产基线：尚未建立。
- current exact-SHA 发布门禁记录：真实安装态 GLM + AgentOS 完整 workflow `1 / 1 passed`；该结果不替代生产基线。
- v0.3.1 发布后 30 天目标：`>= 95%`，weekly 复核。

## 3. 6W2H

| 问题 | 定义 |
|---|---|
| Who | 需要在桌面上记录会议、检索资料、生成办公产物并委托长任务的个人用户；Android / TV 是受限会议入口。 |
| What | 将会议、知识、任务、产物、分享和诊断组织为可恢复 workflow。 |
| Where | Electron Desktop Pro 为主；Android / TV / public demo 使用受限远程模式。 |
| When | 会前准备、会议中转写、会后整理、资料问答、产物生成与后台任务执行。 |
| Why | 避免转写、AI 回答、纪要、Todo 与文件分散在不同状态源，导致重启丢失、失败不可重试、归属不清和 public 数据串用。 |
| How | local-first 客户端 + FastAPI + SQLite Workflow Kernel + owner-scoped RAG + Agent runner bridge。 |
| How Much | v0.3.1 聚焦现有产品闭环与跨端发布，不引入团队协作或云同步产品面。 |
| How Well | 可恢复、可重试、可追溯；public 资源按 principal 隔离；UI 清晰且响应式；门禁有反例测试和真实安装态验证。 |

## 4. 用户与场景

### 4.1 Desktop Pro 用户

用户在自己的 Mac、Windows 或 Linux 机器上使用完整能力：

- 本机会议和持续采集；
- 本机工作区资料；
- 本机 SQLite 与文件存储；
- 用户明确授权后的 Full Access Agent；
- 本机 Artifact 打开、导出与诊断。

Desktop Pro 是受信任的单机边界。LLM 生成代码在宿主机执行、Electron IPC 暴露本机能力、Agent 扫描授权目录，都是该模式的有意能力，不被包装成 public 多租户能力。

### 4.2 Public / Android / TV 用户

用户通过服务端签发的设备身份和 session 访问远程能力：

- 会议采集与当前会议；
- owner-scoped 历史、RAG、Artifact 和 Workflow；
- 受配额与 admission 控制的模型调用；
- 不获得 host-admin、桌面文件系统或 Full Access Agent 创建能力。

## 5. 产品原则

1. **从用户任务出发**：界面展示会议、知识、任务和产物，不展示底层表、内部 ID 或异常类名。
2. **一个事实一个 Owner**：domain、Workflow、RAG 和 UI 各自有明确事实源。
3. **失败可见且可行动**：失败状态就近提供原因、重试或恢复路径。
4. **重启不是丢失**：有持久副作用的流程必须能恢复或明确收口。
5. **local 与 public 不混淆**：模式由可信边界决定，renderer 不能凭 URL 猜测。
6. **隔离默认生效**：public 数据访问必须使用服务端验证的 principal scope。

## 6. 核心用户 Workflow

### 6.1 会议到纪要

1. 用户开始会议或由采集上下文进入会议。
2. 系统持续转写并显示说话人。
3. 用户结束会议。
4. backend 创建 meeting finalize workflow。
5. 纪要成功后持久化并投影到 Inspector；失败时显示可重试状态。
6. 用户显式清除纪要后写 tombstone，恢复任务不得重新生成。

### 6.2 资料到回答

1. 用户授权工作区或上传文件。
2. 系统以 owner scope ingest，并记录 RAG lifecycle 和 quota。
3. 用户提问，SSE 返回回答、引用与 `done` 终帧。
4. 用户断开时取消上游；没有 `done` 不显示“已回答”。
5. 删除或重新扫描后，所有 backend 实例通过同一 revision/manifest 看见变更。

### 6.3 纪要到产物

1. 用户从 CommandBar 或 Todo 发起 PPT、Word、Excel、HTML 等生成。
2. 系统创建 Artifact workflow，并在受控 staging 中生成文件。
3. 成功时在同一 Unit of Work 中提交 Artifact metadata、link、run/event 和 outbox。
4. 失败时保留可重试状态；重试创建新 run 并保留 lineage。
5. 用户可以预览、下载或在系统中打开产物。

### 6.4 Agent 长任务

1. Desktop 用户选择 workspace 并显式授予 Full Access。
2. 系统创建 Agent task 与关联 Workflow run。
3. runner event 持久化并投影为用户可读进度。
4. 任务支持取消、超时、重试、跨重启恢复和 bounded Artifact import。
5. terminal first-wins，晚到的冲突事件不能改写已确认结果。

### 6.5 Public 身份连续性

1. 新设备 enroll，服务端签发 device credential 与 session。
2. 客户端安全保存身份材料并续签 session。
3. rotation 使用 begin/commit/abort 语义避免崩溃丢失身份。
4. 401 停止继续使用失效身份；409 提示身份冲突，不静默创建另一个 owner。
5. 用户可以撤销 session family 或设备。
6. 公共服务要求客户端自报包内版本；缺失、非法或低于 `0.3.1` 的 HTTP 请求返回 426，WebSocket 以 4426 关闭，并停止身份、业务和重连尝试。
7. Electron 主进程签发的 session 必须携带并匹配 `backend_origin`；renderer 切换后端时先清 owner-scoped UI/WS 状态，绝不把 A 后端 bearer 发送给 B。

## 7. 功能需求

### FR-1 会议与采集

- 支持开始、结束、续接和恢复会议。
- 当前 owner 同时最多一个 active meeting。
- 转写、说话人、纪要、分钟状态和 RAG projection 持久化。
- 上传有超时、大小、背压和 scope 边界。

### FR-2 Knowledge / RAG

- ingest、delete、workspace scan、meeting projection 进入 durable workflow。
- ask 是随连接取消的无副作用 SSE 读流。
- SQLite manifest/revision 是跨进程提交点，JSON 仅为可重建 cache。
- 查询和删除只能访问当前 tenant / owner 的内容。

### FR-3 Workflow 与 Artifact

- Workflow 支持 idempotency、revision、deadline、cancel、retry、event replay 和 recovery。
- domain write、run/event 与 outbox 同事务。
- Artifact 必须登记 metadata 和来源 link；public 下载不能依赖可猜路径。
- meeting、Todo、Artifact 和 Agent 的状态在重启后仍可恢复。

### FR-4 Agent

- 保留 Claude Code / AgentOS Full Access 主路径。
- grant 与 task 绑定当前 owner/device；public 普通 principal 无权创建。
- bridge 支持 lease、heartbeat、backoff 和接管。
- Artifact import/proxy 必须限制路径、大小和流式读取。

### FR-5 Public 身份与隔离

- 服务端签发并验证 tenant、user、device、session。
- Meeting、RAG、Artifact、Workflow、Agent、WS 和 storage 使用 owner scope。
- session 支持 enroll、renew、claim、credential rotate、additional device 和 revoke。
- admission、quota 与敏感操作 rate limit 失败必须 fail closed。

### FR-6 Desktop UX

- 左侧 Session Navigation、中间 Workbench、右侧 Inspector。
- 转写 / 助手、纪要 / 工作产物分别切换，不混排。
- 全局使用一套 Codex-like 系统字体和统一线性图标。
- 关键图标按钮有可访问名称；长文本、token、标题和路径有明确换行或省略规则。
- 411、960、1280 和 1920 宽度下无页面级横向溢出。

### FR-7 诊断与发布

- health、diagnostics 和日志不泄露 bearer、query secret、URL userinfo 或绝对敏感路径。
- 桌面安装包携带对应平台 backend binary。
- Android / TV public 资产使用稳定 release 身份签名并校验产物。
- 未满足平台签名条件时 public publish fail closed。

## 8. 非功能需求

| 维度 | 要求 |
|---|---|
| 一致性 | 永久状态分裂为 0；已知瞬时投影差异必须可恢复。 |
| 隔离 | public 跨 principal 读取、写入、订阅成功数为 0。 |
| 恢复 | 崩溃、重启、lease 过期和 outbox consumer 失败后可重放或收口。 |
| 可观测 | 失败有稳定错误分类、run/event lineage 和诊断证据。 |
| 可访问 | 关键操作支持可访问名称、焦点恢复和键盘路径。 |
| 可发布 | 版本、lock、SBOM、签名、安装 smoke 与 rollback 分别有门禁。 |

## 9. 非目标

- 团队组织、邀请、角色权限和跨 tenant 分享。
- 多人共同编辑同一会议或知识库。
- 自动云同步本机 workspace。
- 把 public backend 变成任意宿主机命令执行服务。
- 用 mock、health probe 或单条 smoke 代替真实业务合同。

## 10. 已知 P2 与后续需求

1. SQLite 长期元数据尚无统一生命周期预算：RAG blob、ambient WAV 与 transcript inject 已计量，但 meeting、workflow、event 等仍依赖运维 retention。

## 11. v0.3.1 验收

- Backend：`1045 collected`，`18 live deselected`，确定性 `1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`，line coverage `87.46%`（终端显示 `87%`），进程自然退出；Ruff check、Ruff format `250 files`、mypy `128 source files`、compile 通过。
- Electron main-process contracts：`177 / 177 passed`；Desktop E2E：`150 passed`；scenarios：`29 passed`。
- Public isolation self-test 与双 principal 完整 smoke 通过；release aggregate `31 / 31 passed`，actionlint 与 action pins 通过。
- Android / TV current exact-SHA phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，另有 Capacitor `Hint 2`。debug APK 不可作为公开发布资产。
- npm 两处审计为 `0`；Python six locks 均有效，runtime/dev/build 各保留同一项上游无 `fix_versions` 的 `torch` `CVE-2025-3000` 受控例外至 2026-08-12，lint/typecheck/audit-tool 为 `0`，不得把 Python 总体结果标成 clean 或零漏洞。

以上 current exact-SHA 本地门禁由 [F-ECHO-028] 记录。macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 通过；read-only DMG smoke `1 / 1`、安装态完整 workflow `1 / 1` 与 live contract `2 / 2` 均通过，`0 skipped / 0 failed`。安装态覆盖真实下载 `0600`、marker、安全文件名、无 partial、GLM/RAG、失败注入、重启、retry 和 AgentOS success/cancel/timeout/restart。Developer ID、notary、staple 与 Gatekeeper 正式链路仍为 external skipped。

截至 2026-07-13，公共 Release / 生产 / bootstrap 仍分别为 `v0.2.50` / `0.2.49` / `0.2.45`，bootstrap 未声明 `minimum_client_version` [F-ECHO-029]。正式跨平台签名、受保护 environment/secret、Release 与公网部署仍必须以各自实际结果为准。
