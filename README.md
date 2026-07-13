# EchoDesk · 数字分身工作台

EchoDesk 是面向会议与办公场景的本地优先桌面应用：持续采集和会议转写进入知识库，用户可以基于会议与资料检索、生成纪要和办公产物，并把长任务交给 Agent 执行。当前源码版本为 **v0.3.2**。

v0.3.2 新增 L0-L3 分层 memory、ASR 文本关联与信息流来源卡，快速任务实际使用 Yunwu `gpt-5.4-nano`（界面显示 `qwen3 8b`），Echo AI 主回答继续使用 Yunwu `deepseek-v4-flash`；同时加入 app/backend build 握手、数据库 schema fail-closed、会议自动结束/cooldown 与 FireRed 复读/幻觉过滤。

- 桌面端默认启动安装包内自带的本机 backend，业务数据写入本机 SQLite。
- public demo 只有在发布入口显式设置 `ECHO_PUBLIC_DEMO=1` 时启用；Android / TV 作为受限客户端连接配置的服务端。
- 0.3.1 公共后端最低客户端版本为 0.3.1；v0.2.50 不含设备 session 协议，首次公网切换是明确的 breaking cutover，必须先发布可安装的 0.3.1 GitHub prerelease。Release 渠道可标 prerelease，但包内客户端必须自报稳定版本串 `0.3.1`，不能自报 `0.3.1-rc.1`。
- 公开下载以 [GitHub Releases](https://github.com/yoligehude14753/echo-demo/releases/latest) 实际列出的资产为准。CI artifact、本机构建产物和源码版本都不自动等同于已公开发布。

## 北极星指标

> **已授权 Workflow 有效闭环率**：进入终态的非用户主动取消 workflow 中，最终为 `succeeded` 的比例；按 tenant / owner 分域统计，不跨主体聚合原始内容。
>
> 当前生产基线：尚未建立。v0.3.1 current exact-SHA 安装态完整工作流 `1 / 1 passed`；该结果是发布门禁证据，不代替生产基线。
>
> v0.3.1 发布后 30 天目标：`>= 95%`；测量频率：weekly。完整口径与护栏见 [`METRICS.md`](METRICS.md)。

## 当前状态

v0.3.1 当前本地源码与受控安装态证据 [F-ECHO-028]：

- Backend：`1045 collected`，其中 `18` 条 live contract 明确分流；确定性门禁 `1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`，line coverage `87.46%`（终端显示 `87%`），pytest 进程自然退出。Ruff check、Ruff format `250 files`、mypy `128 source files` 与 compile 均通过。
- Desktop：Electron main-process contracts `177 / 177 passed`；Playwright E2E `150 passed`；业务 scenarios `29 passed`。
- Public isolation：self-test 与双 principal 完整 smoke 均通过；release aggregate `31 / 31 passed`，actionlint 与 action pin 检查通过。
- Android / TV：current exact-SHA phone 与 TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint 为 `Fatal 0 / Error 0 / Warning 0`，另有 Capacitor `Hint 2`。产物是 debug APK，不可作为公开发布资产。
- 依赖审计：desktop npm 与内置 `ppt_ib_deck` npm 均为 `0` finding。Python six locks 均有效；runtime/dev/build 三份锁各仍报告同一项上游无 `fix_versions` 的 `torch` `CVE-2025-3000`，按文档化例外控制至 2026-08-12；lint/typecheck/audit-tool 锁为 `0` finding，不能把 Python 总体结果写成 clean 或零漏洞。
- 版本：v0.3.2 源码中的 Desktop、Backend、Android、package-lock、Commitizen 和版本契约已统一；下述安装态结果仍是 v0.3.1 的历史门禁证据，不冒充 v0.3.2 已验证结果。

current exact-SHA macOS arm64 门禁已通过：fresh ad-hoc DMG、ZIP、metadata、blockmap、codesign、plist、asar、forbidden-file scan、`1066` 组件 SBOM 与 SHA-256 校验通过，read-only mounted DMG smoke `1 / 1 passed`；安装态完整 workflow `1 / 1 passed`，覆盖真实下载 `0600`、marker、安全文件名、无残留 partial，以及 GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart；live contract `2 / 2 passed`，`0 skipped / 0 failed`。Developer ID、notary、staple 与 Gatekeeper 正式链路因缺外部签名输入而 skipped，不能由 ad-hoc 结果替代。

截至 2026-07-13 的公共状态仍是 GitHub Release `v0.2.50`、生产 backend `0.2.49`、bootstrap `app_version=0.2.45` 且没有 `minimum_client_version` [F-ECHO-029]。正式跨平台签名、受保护 environment/secret、公开 Release 和公网切流仍是外部阻塞，必须分别以对应工作流、签名校验与部署验收结果为准。

## 产品主线

```text
会议输入 -> 知识沉淀 -> 任务执行 -> 产物生成 -> 分享归档 -> 诊断恢复
```

核心能力：

- 会议采集、转写、说话人识别、纪要生成和失败重试；纪要生成由当前 Workflow run 持有，取消、超时或失败与会议可见终态在同一事务收口，显式 retry 才能由新 run 接管。
- 工作区资料与会议内容统一进入 owner-scoped RAG；meeting 投影用 generation + index/delete fence 抵御迟到写，ambient 片段用稳定 operation id 和持久退避队列完成重启修复。
- Artifact、Todo、Meeting finalize 与 Agent task 进入统一 Workflow Kernel。retry 的 child、parent lineage event、outbox 与 domain marker 在一个 Unit of Work 内提交，并与同 scope 的 fresh create 通过 `active_key` 仲裁唯一活动 run。
- public backend 使用服务端签发的 tenant / user / device / session principal，隔离会议、RAG、Artifact、Workflow、Agent 与 WebSocket；匿名 session credential body 使用独立的全局/peer admission pool，慢请求不能占满普通已认证业务通道。
- Electron 桌面端采用 Session Navigation、Workbench、Inspector 三层信息架构；public workspace 传输把 HTTPS backend origin、credential vault、session 和 origin-scoped registry 绑定为同一边界，切换后取消旧操作且不复用旧 bearer/文档登记。

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
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8769 --ws-max-size 4096
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
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
export ECHO_USER_DIR="$TEST_ROOT"
export DB_PATH="$TEST_ROOT/echodesk.db"
export STORAGE_DIR="$TEST_ROOT/storage"
export RAG_INDEX_DIR="$TEST_ROOT/rag_index"
(cd backend && .venv/bin/pytest tests -m "not live")

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
- [`docs/0.3/README.md`](docs/0.3/README.md)：0.3 文档索引与当前/最终证据边界。
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
