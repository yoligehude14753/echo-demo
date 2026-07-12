# EchoDesk · 数字分身工作台

EchoDesk 是面向会议与办公场景的本地优先桌面应用：持续采集和会议转写进入知识库，用户可以基于会议与资料检索、生成纪要和办公产物，并把长任务交给 Agent 执行。当前源码版本为 **v0.3.1**。

- 桌面端默认启动安装包内自带的本机 backend，业务数据写入本机 SQLite。
- public demo 只有在发布入口显式设置 `ECHO_PUBLIC_DEMO=1` 时启用；Android / TV 作为受限客户端连接配置的服务端。
- 公开下载以 [GitHub Releases](https://github.com/yoligehude14753/echo-demo/releases/latest) 实际列出的资产为准。CI artifact、本机构建产物和源码版本都不自动等同于已公开发布。

## 北极星指标

> **已授权 Workflow 有效闭环率**：进入终态的非用户主动取消 workflow 中，最终为 `succeeded` 的比例；按 tenant / owner 分域统计，不跨主体聚合原始内容。
>
> 当前生产基线：尚未建立。v0.3.1 的安装态真实工作流验收为 `1 / 1`，仅是发布门禁证据，不代替生产基线。
>
> v0.3.1 发布后 30 天目标：`>= 95%`；测量频率：weekly。完整口径与护栏见 [`METRICS.md`](METRICS.md)。

## 当前状态

v0.3.1 已完成源码与本机安装态收口，最终验证证据为：

- Backend：`916 collected`，其中 `18` 条 live contract 明确分流；确定性门禁 `898 passed / 0 skipped`，coverage `87%`，pytest 进程自然退出。
- Live model contract：GLM 产品契约 `2 / 2 passed`。
- Desktop：Electron main-process contracts `70 passed`；Playwright E2E `95 passed`；业务 scenarios `29 passed`。
- 安装态：真实 GLM + AgentOS 完整 workflow `1 / 1 passed`；packaged local smoke 通过。
- 版本：Desktop、Backend、Android、package-lock、Commitizen 和安装态断言统一为 `0.3.1`。

以上是当前分支的验证状态。跨平台 CI、公开 Release 和公网切流必须分别以对应工作流、签名校验与部署验收结果为准。

## 产品主线

```text
会议输入 -> 知识沉淀 -> 任务执行 -> 产物生成 -> 分享归档 -> 诊断恢复
```

核心能力：

- 会议采集、转写、说话人识别、纪要生成和失败重试。
- 工作区资料与会议内容统一进入 owner-scoped RAG；查询、增删与重启恢复使用同一索引事实源。
- Artifact、Todo、Meeting finalize 与 Agent task 进入统一 Workflow Kernel，支持幂等、超时、取消、重试、事件 replay 和恢复。
- public backend 使用服务端签发的 tenant / user / device / session principal，隔离会议、RAG、Artifact、Workflow、Agent 与 WebSocket。
- Electron 桌面端采用 Session Navigation、Workbench、Inspector 三层信息架构，统一系统字体、状态、图标与响应式行为。

## 运行模式

| 模式 | 启用方式 | Backend | 数据边界 |
|---|---|---|---|
| Desktop Pro | 默认 | 安装包内本机 backend | 本机 `~/.echodesk/` |
| Public demo | `ECHO_PUBLIC_DEMO=1` | 配置的 HTTPS 服务 | 服务端 principal scope |
| 强制本机兼容 | `ECHO_FORCE_LOCAL_BACKEND=1` | 本机 backend | 覆盖 public 开关 |
| Android / TV | 移动端安装包 | 配置的 HTTPS 服务 | 设备身份 + 服务端 scope |

本机模式是受信任的单机能力边界：用户授权后，Agent 可以执行宿主机任务，Electron IPC 可以访问明确暴露的本机能力。public 普通 principal 不获得 host-admin 能力。

## 快速开始

要求：Python 3.11、Node.js 24。

```bash
# Backend
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install --require-hashes -r backend/requirements-dev.lock
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8769
```

另开终端：

```bash
cd desktop
npm ci
npm run dev
```

构建自包含桌面安装包：

```bash
cd desktop
npm run app:dist:mac       # macOS arm64
npm run app:dist:win       # Windows x64 runner
npm run app:dist:linux     # Linux x64 runner
```

安装包会携带对应平台的 backend binary；正常用户不需要先运行旧的 `install-backend.sh`。该脚本只保留给源码部署与旧安装迁移。

## 质量门

```bash
# 供应链与版本
node scripts/check-npm-lock-registries.cjs
python3 scripts/check-ci-action-pins.py
python3 scripts/check-python-locks.py
node desktop/scripts/check-version-sync.cjs

# Backend
backend/.venv/bin/ruff check backend
backend/.venv/bin/ruff format --check backend
backend/.venv/bin/mypy backend/app
cd backend && .venv/bin/pytest tests -m "not live"

# Desktop
cd desktop
npm run test:electron
npm run lint
npm run typecheck
npm run build
CI=1 NODE_ENV=test npm run e2e
CI=1 NODE_ENV=test npm run scenarios
```

live contract 与安装态 workflow 是独立门禁，不能用 `/healthz`、mock E2E 或 packaged smoke 代替。

## 文档

- [`PRD.md`](PRD.md)：产品目标、用户流程、范围与验收。
- [`METRICS.md`](METRICS.md)：北极星、输入指标和护栏指标。
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：当前实现架构、事务边界与已知 P2。
- [`docs/0.3/README.md`](docs/0.3/README.md)：0.3 文档索引与最终门禁。
- [`docs/INSTALL.md`](docs/INSTALL.md)：安装、运行模式、签名与排障。
- [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)：可复跑演示流程。
- [`CHANGELOG.md`](CHANGELOG.md)：用户可见变更。

## 技术栈

- Desktop：Electron、React 18、TypeScript、Vite、Ant Design 5、Playwright。
- Backend：FastAPI、Pydantic、SQLite、aiosqlite、Ports & Adapters。
- Retrieval：jieba、BM25、持久化 scope 文档与协调索引。
- Packaging：PyInstaller、electron-builder、Capacitor / Android Gradle。

## License

Proprietary
