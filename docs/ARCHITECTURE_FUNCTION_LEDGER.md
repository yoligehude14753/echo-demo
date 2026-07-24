# EchoDesk 架构与功能台账

> 本文件是 EchoDesk 唯一的架构、功能与缺陷同步台账。
>
> 适用快照：codex/echodesk-v0.3.2-capture-refresh-fix @ e8e0258f3f84c0a27adb0ff07ea3976fd8d7d96a
> 观察日：2026-07-24
> 事实边界：源代码存在不等于真实服务、真实设备或安装包已验收。

本文件不是 PRD、测试报告或发布公告。

它记录当前工作树的结构、功能状态、证据边界和已知阻塞。

它不记录密钥、用户数据、机器 IP、访问令牌或其他敏感运行信息。

## 1. 强制维护协议

每次下列任一变化，提交同一变更前必须同步本文件：

1. 新增、变更或删除架构层、跨层边界、存储边界或部署模式。
2. 新增、变更、下线或拆分任何用户可见功能。
3. 发现、确认、修复、缓解或关闭任何 bug、风险和外部阻塞。
4. 变更功能的验证层级、运行模式、发布边界或安全决策。

每个条目必须同时更新以下四处：

1. 架构总览中的受影响层和代码锚点。
2. 功能矩阵中的状态、证据、验证等级和日期。
3. 当前缺陷/风险中的结论、影响和下一步。
4. 附录的 append-only 变更记录。

每个条目至少绑定：

- 源码锚点或其他可复核证据。
- 验证等级：源码检查、自动化测试、运行时、安装包。
- 最近观察或变更日期。
- 适用的 FactStore fact_id；过期事实必须显式标为不可作当前验收。

状态字段只允许使用下列值：

| 状态 | 含义 |
|---|---|
| 已实现（源码） | 当前工作树存在实现；不推断运行、服务或安装包结果。 |
| 已验收（注明层级） | 必须写明源码、测试、运行时或安装包层级及证据位置。 |
| 开发中 | 工作树中有未闭环设计或实现；不可当交付结论。 |
| HOLD | 因安全决策、外部依赖或待决策冲突暂停；不得自行合并。 |
| 未实现/待数据 | 没有实现，或缺少必要数据、授权、环境或样本。 |
| 已废弃 | 不再作为当前路径；保留迁移或替代关系。 |

验证等级不可以倒推状态。

例如，单元测试源码存在只能说明可测试设计存在；没有执行记录时不得写成已验收。

运行时或安装包结论必须记录环境、命令、时间、结果和可复查产物位置。

纠正旧结论不得覆盖历史记录，必须在变更记录中追加新条目，并通过 FactStore event 表达事实变化。[F-ECHO-040]

## 2. 当前证据边界

- 本台账只管理上述快照及其后续工作树变化，不替代 README、历史 0.3 文档或 GitHub Release 文案。
- 当前工作树含 ASR router、StepFun、遥测和 Electron transport 的未提交改动；这些改动只有源码与新增测试源码证据，未取得运行时或安装包验收。[F-ECHO-039]
- F-ECHO-035、F-ECHO-036、F-ECHO-037 的原始证据在 _state/events/2026-07-13T1810_echodesk-0-3-2-runtime-memory.yaml。
- 上述三个 FactStore facts 在本观察日已超过其 TTL，不能用作当前运行或安装验收；本台账仅据其说明历史源码范围。
- _state/FACTS.yaml 曾与 event 流存在项目路径、fact_count 及 F-ECHO-035 至 F-ECHO-037 的物化失步；本次通过 event/replay 纠正物化视图。[F-ECHO-038]
- 工作分支与 origin/main 的关系只代表本次观察快照，不能据此推断已合并、已发布或可回滚状态。

## 3. 架构总览

### 3.1 客户端层

- 职责：Electron main process、React renderer、会话 transport、WebSocket、采集队列和工作区界面。
- 代码锚点：desktop/electron/main.cjs、desktop/electron/backend-endpoint.cjs、desktop/src/App.tsx、desktop/src/session.ts、desktop/src/ws.ts、desktop/src/capture/、desktop/src/components/WorkspaceBar.tsx。
- 状态：已实现（源码）。
- 验证：当前仅记录源码边界；本快照中的 transport/采集改动另见“当前开发中”。[F-ECHO-039]

### 3.2 Android / TV 客户端

- 职责：Capacitor WebView 容器、Android 与 TV manifest/Gradle、设备端受限连接。
- 代码锚点：desktop/capacitor.config.ts、desktop/android/app/build.gradle、desktop/android/app/src/main/AndroidManifest.xml。
- 状态：已实现（源码）。
- 验证：跨平台构建、签名和真机安装必须分别记录，不能由源码或历史发布叙述替代。

### 3.3 FastAPI API 层

- 职责：HTTP、WebSocket、会话、会议、采集、问答、检索、产物、Agent、管理和诊断入口。
- 代码锚点：backend/app/main.py、backend/app/api/、backend/app/api/ws.py、backend/app/api/deps.py。
- 状态：已实现（源码）。
- 边界：API 层只编排请求、principal 与依赖，不应成为业务事实源。

### 3.4 用例与 Workflow 层

- 职责：ambient capture、会议状态、会议流水线、问答、检索、意图、语音、产物与工作流状态机。
- 代码锚点：backend/app/use_cases/、backend/app/workflows/kernel.py、backend/app/workflows/service.py。
- 状态：已实现（源码）。
- 边界：跨域终态、重试、取消和恢复应由 Workflow 及其持久化记录收口。

### 3.5 Ports / Adapters 层

- 职责：通过 Port 隔离 SQLite、LLM、STT、TTS、RAG、技能执行、Agent、Web search 与事件总线。
- Port 锚点：backend/app/ports/repository.py、llm.py、stt.py、tts.py、rag.py、skill.py、web_search.py、event_bus.py。
- Adapter 锚点：backend/app/adapters/repo/sqlite.py、llm/openai_compatible.py、stt/、tts/qwen3_tts.py、rag/bm25.py、skill/、web_search/tavily.py、event_bus/inmemory.py。
- 状态：已实现（源码）。
- 边界：外部服务、模型和数据库实现不得被上层用例直接替代。

### 3.6 安全与 principal scope

- 职责：local/public principal、tenant/owner/device/session scope、访问控制、配额、路径与凭据边界。
- 代码锚点：backend/app/security/context.py、models.py、scope.py、sessions.py、access.py、governor.py、desktop/electron/public-identity-session.cjs。
- 状态：已实现（源码）。
- 边界：public 普通 principal 不取得本机 host-admin 能力；模式变化必须经过显式安全决策。

### 3.7 数据、文件、事件与 WebSocket

- 职责：SQLite 权威记录、受 scope 约束的文件目录、事务 outbox、进程内 event bus、WebSocket 投影与恢复。
- 代码锚点：backend/app/adapters/repo/sqlite.py、backend/app/adapters/repo/migrations/014_workflow_kernel.sql、028_workflow_outbox_consumers.sql、034_workflow_outbox_scope_lanes.sql、backend/app/adapters/event_bus/inmemory.py、backend/app/api/ws.py。
- 状态：已实现（源码）。
- 边界：前端内存和 WebSocket 事件不是权威业务记录；恢复需回到 SQLite/outbox。

### 3.8 打包与发布层

- 职责：Electron 自包含 desktop、PyInstaller backend、Capacitor Android/TV、版本和签名门禁。
- 代码锚点：desktop/package.json、desktop/electron/release-assets.cjs、desktop/electron/backend-contract.cjs、desktop/android/app/build.gradle。
- 状态：已实现（源码）。
- 验证：签名、notarization、Authenticode、安装态及真实服务验收均独立于构建源码。

## 4. 功能矩阵

| 功能域 | 范围 | 状态 | 源码锚点 | 证据/验证边界 | 日期 |
|---|---|---|---|---|---|
| Ambient capture | 音频采集、预过滤、落盘、分段与状态统计 | 已实现（源码） | backend/app/use_cases/ambient_capture.py；desktop/src/capture/ | 当前 WIP 传输改动仅源码/新增测试，见第 5 节。[F-ECHO-039] | 2026-07-24 |
| 会议 | 自动/手动会议、生命周期、转写、纪要与重试 | 已实现（源码） | backend/app/use_cases/meeting_state.py；meeting_pipeline.py；backend/app/api/meetings.py | 0.3.2 会议 lifecycle 为源码已实现；运行/安装验收待复验。[F-ECHO-037，已过 TTL] | 2026-07-24 |
| 转写与声纹 | FireRed、音频门、说话人分段和注册 | 已实现（源码） | backend/app/adapters/stt/firered.py；audio_gate.py；adapters/diarizer/ecapa.py | FireRed filters 为源码已实现；运行/安装验收待复验。[F-ECHO-037，已过 TTL] | 2026-07-24 |
| 工作区 / RAG | 资料接入、解析、BM25、scope 文档与索引生命周期 | 已实现（源码） | backend/app/adapters/rag/；backend/app/api/workspace.py；retrieval.py | 仅源码边界；工作区刷新 WIP 见第 5 节。 | 2026-07-24 |
| 问答 / Intent / TTS | 意图路由、检索问答、语音合成与播放 | 已实现（源码） | backend/app/use_cases/intent_router.py；retrieve_and_answer.py；speak.py；adapters/tts/qwen3_tts.py | 仅源码边界。 | 2026-07-24 |
| 模型路由 | 快速任务、主回答和 LLM router | 已实现（源码） | backend/app/adapters/intent/llm_router.py；backend/app/config.py | 0.3.2 模型路由为源码已实现；运行/安装验收待复验。[F-ECHO-036，已过 TTL] | 2026-07-24 |
| Memory L0-L3 / provenance | 分层记忆、抽取、召回、来源卡和管理 API | 已实现（源码） | backend/app/memory/；backend/app/api/memory.py；desktop/src/components/TranscriptStream.tsx | 0.3.2 memory/provenance 为源码已实现；运行/安装验收待复验。[F-ECHO-035，已过 TTL] | 2026-07-24 |
| Artifact / Todo / skills | 产物仓储、生成、恢复、待办与技能执行 | 已实现（源码） | backend/app/artifacts/；use_cases/generate_artifact.py；adapters/skill/ | 仅源码边界。 | 2026-07-24 |
| Agent | Agent task、授权、事件桥、产物导入与 command outbox | 已实现（源码） | backend/app/agents/；backend/app/api/agents.py | 仅源码边界；真实外部 Agent 运行需单独验收。 | 2026-07-24 |
| Workflow / outbox / 恢复 | run、active_key、重试、事务 outbox、消费者与恢复 | 已实现（源码） | backend/app/workflows/；migrations/014_workflow_kernel.sql；028_workflow_outbox_consumers.sql；034_workflow_outbox_scope_lanes.sql | 仅源码边界。 | 2026-07-24 |
| WS / desktop UX | 实时事件、重连、rehydrate、Session Navigation、Workbench、Inspector | 已实现（源码） | backend/app/api/ws.py；desktop/src/ws.ts；desktop/src/session.ts；desktop/src/App.tsx | 本轮 header/backpressure/刷新改动尚未运行验收。[F-ECHO-039] | 2026-07-24 |
| public identity / 隔离 | server-issued identity、scope 隔离、credential vault、public workspace transport | 已实现（源码） | backend/app/security/；backend/app/api/sessions.py；desktop/electron/public-identity-session.cjs | 打包模式冲突处于 HOLD，见第 5 节。 | 2026-07-24 |
| 诊断 / 管理 | health、capture stats、诊断导出、管理端点与 STT telemetry 查询 | 已实现（源码） | backend/app/api/health.py；diagnostics.py；admin.py | 新 telemetry migration/HTTP+WS E2E 待验。 | 2026-07-24 |
| build / schema fail-closed | app/backend contract、迁移检查和版本一致性 | 已实现（源码） | backend/app/build_contract.py；backend/app/adapters/repo/migrator.py；desktop/electron/backend-contract.cjs | 0.3.2 fail-closed 为源码已实现；运行/安装验收待复验。[F-ECHO-037，已过 TTL] | 2026-07-24 |
| 跨平台打包 | macOS、Windows、Linux、Android、TV 构建与资产规则 | 已实现（源码） | desktop/package.json；desktop/electron/release-assets.cjs；desktop/android/ | 真实签名、安装和商店/发布状态不由此行断言。 | 2026-07-24 |

## 5. 当前开发中与 HOLD

| 主题 | 状态 | 范围与代码锚点 | 当前证据 | 验收缺口 | 日期 |
|---|---|---|---|---|---|
| FireRed 主 + StepFun candidate、多 provider router | 开发中 | backend/app/adapters/stt/router.py；stepfun.py；__init__.py；backend/app/config.py | 源码与新增单测存在。[F-ECHO-039] | 真实 StepFun 调用、故障切换、balance、并发、queue、breaker 未验收。 | 2026-07-24 |
| Ambient router metadata 与匿名日聚合 telemetry | 开发中 | backend/app/use_cases/ambient_capture.py；backend/app/adapters/repo/migrations/040_stt_usage_telemetry.sql；backend/app/api/admin.py | 源码与 migration 存在。[F-ECHO-039] | migration、HTTP+WS E2E 与隐私边界运行验收待做。 | 2026-07-24 |
| 客户端平台/version header、采集背压与刷新防旧响应覆盖 | 开发中 | desktop/src/session.ts；desktop/src/ws.ts；desktop/src/capture/；desktop/src/components/WorkspaceBar.tsx | 新增测试源码可见，未读取运行记录。[F-ECHO-039] | desktop 真实运行与安装包验收待做。 | 2026-07-24 |
| packaged Electron public-first | HOLD | desktop/electron/backend-endpoint.cjs；desktop/electron/main.cjs | WIP 将发布包改为 public-first，仅 ECHO_FORCE_LOCAL_BACKEND 选择本机。 | 与 README.md、ARCHITECTURE.md、docs/0.3/ARCHITECTURE.md 的 local-first 叙述冲突；这是安全/产品架构决策，不能自行合并。 | 2026-07-24 |

## 6. 当前缺陷与阻塞

| 编号 | 状态 | 影响与源码证据 | 处置边界 | 日期 |
|---|---|---|---|---|
| BUG-WIP-001 | 开发中 | STT_BACKEND=stepfun_ws 或 stepfun_sse 被列为支持值，但 _build_providers 只在 requested 包含 stepfun 时实例化 provider，别名会形成无候选 router。锚点：backend/app/adapters/stt/__init__.py:24-33、54-64。 | 未验收；修复前不可交付；本台账任务不修改实现。 | 2026-07-24 |
| BUG-WIP-002 | 开发中 | stepfun_silence_duration_ms 与 stepfun_vad_threshold 已进入 StepFunOptions，却未写入 WebSocket session.update 或 SSE payload。锚点：backend/app/config.py:322-335；backend/app/adapters/stt/stepfun.py:61-90、98-124、336-355。 | 未验收；修复前不可交付；本台账任务不修改实现。 | 2026-07-24 |
| BLOCK-ARCH-001 | HOLD | packaged Electron 的 public-first WIP 与根 README、ARCHITECTURE.md、docs/0.3/ARCHITECTURE.md 的 local-first 描述冲突。锚点：desktop/electron/backend-endpoint.cjs:84-102；desktop/electron/main.cjs:146-148；README.md:7-8；ARCHITECTURE.md:11；docs/0.3/ARCHITECTURE.md:179-180。 | 需要明确的安全、产品与发布决策；不能以源码存在或历史文档替代决策。 | 2026-07-24 |

## 7. 当前已知风险与缺口

| 风险/缺口 | 状态 | 证据或边界 | 下一步 |
|---|---|---|---|
| FactStore materialized view 失步 | 开发中 | 观察到 FACTS.yaml 的 project、fact_count 与 F-ECHO-035 至 F-ECHO-037 未物化不一致。[F-ECHO-038] | 每次 event 变更后 replay，并在提交前 health-check。 |
| 过期事实不能作当前验收 | HOLD | F-ECHO-035 至 F-ECHO-037 已过 TTL；历史 event 只说明源码范围。 | 运行、安装或发布结论需新 event 和可复查证据。 |
| 分支与 origin/main 漂移 | HOLD | 仅是本观察日的工作树快照，不等于合并或发布状态。 | 每次同步时重新记录比较命令与结果。 |
| StepFun 外部调用治理 | 开发中 | backend/app/adapters/stt/stepfun.py 直用 httpx 与 websockets；这与 yoli_http 外部 HTTP 统一入口规则不一致，待架构评审。[F-ECHO-039] | 评审 yoli_http 规则、超时、重试、错误分类与凭据边界。 |
| Playwright 固定等待 | 开发中 | 新旧 E2E 中存在 page.waitForTimeout 与固定 setTimeout；例如 desktop/tests/e2e/workspace-knowledge.spec.ts:243、capture-lifecycle.spec.ts:326。 | 以事件/条件等待替换，并记录 CI 稳定性结果。 |
| 真实多人声音阈值标定 | 未实现/待数据 | 声纹阈值和短段策略有源码参数，但没有本观察日的多人、噪声、姿态样本验收。 | 准备获授权样本和分层评测，再记录阈值决定。 |
| macOS / Windows 正式签名 | HOLD | Developer ID/notarization 与 Authenticode 是外部凭据/发布依赖；本文件不推断其状态。 | 取得外部签名输入后，独立完成构建、签名、安装和回滚验证。 |

## 8. 同步操作清单

架构或功能改动提交前：

1. 检查本文件中是否已有对应功能域、风险或 HOLD。
2. 更新代码锚点、状态、证据、验证等级和日期。
3. 对新事实 append _state/events/ 事件；不得直接手改 _state/FACTS.yaml。
4. 执行 replay、health-check 和与改动相称的验证。
5. 在本文件追加变更记录，说明状态变化与未覆盖验证。

bug 处理前：

1. 新问题先进入“当前缺陷与阻塞”，写明影响和最小源码/运行证据。
2. 修复后不删除旧项；追加修复记录，链接回归测试和运行/安装证据。
3. 若结论改变，使用 FactStore supersede/refute event，而非改写旧事实。

发布或安装验收前：

1. 逐项把“已实现（源码）”提升为“已验收（注明层级）”，不可批量提升。
2. 写明 exact SHA、平台、环境、命令、结果、产物位置和失败路径。
3. 签名、线上服务、真机和真实模型调用必须分别记录，不相互替代。

## 9. Append-only 变更记录

| 日期 | 变更 | 影响范围 | FactStore / 证据 |
|---|---|---|---|
| 2026-07-24 | 建立唯一架构与功能同步台账；定义强制同步协议、状态词表、当前架构、功能矩阵、WIP、缺陷与风险。 | EchoDesk 后续全部架构、功能与 bug 变动。 | F-ECHO-038、F-ECHO-039、F-ECHO-040；本文件。 |

后续记录只能向本表追加，不得改写既有日期或结论。
