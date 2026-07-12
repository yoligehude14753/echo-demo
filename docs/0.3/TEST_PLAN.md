# EchoDesk 0.3 测试计划

版本：0.3.1 | 状态：最终门禁 | 更新时间：2026-07-12

## 1. 测试原则

1. 测试用户 workflow，不只测试 HTTP 200。
2. deterministic 与 live 明确分流；deterministic skip 视为失败。
3. happy、sad、boundary、并发和 crash-recovery 都要有反例。
4. mock E2E、scenario、packaged smoke、installed full workflow 和 public isolation smoke 互不替代。
5. 测试使用隔离 user dir、SQLite、storage 和模型配置，不读写真实 `~/.echodesk`。
6. JUnit、trace、video、coverage 等是 CI artifact，不提交源码仓库。

## 2. 测试层次

| 层次 | 目标 | 入口 |
|---|---|---|
| Unit | 状态机、repo、identity、错误与边界 | `backend/tests/unit` |
| Architecture | route、layer、Workflow/IPC contract | `backend/tests/arch`、Electron tests |
| Deterministic integration | 多组件但不访问 live provider | `backend/tests/integration` 中非 live |
| Live contract | 当前配置的真实 OpenAI-compatible 主模型 | `test_product_model_live.py` |
| Desktop mock E2E | UI、transport、WS、responsive、accessibility | `desktop/tests/e2e` |
| Business scenarios | 可见点击、录像、sad path | `desktop/tests/scenarios` |
| Packaged smoke | bundled backend、端口、版本、持久化、点击 | `packaged-local-smoke.spec.ts` 与平台脚本 |
| Installed full workflow | 真模型、故障注入、重启、retry、Agent | `installed-local-workflow.spec.ts` |
| Public isolation | 双 principal 负例与 cleanup | `scripts/public-isolation-smoke.py` |
| Android / TV | build、identity instrumentation、安装 | Gradle + emulator / device |

## 3. 依赖与供应链门禁

```bash
node scripts/check-npm-lock-registries.cjs
python3 scripts/check-ci-action-pins.py
python3 scripts/check-python-locks.py
node desktop/scripts/check-version-sync.cjs
```

要求：

- GitHub Actions 使用 immutable commit SHA。
- npm lock 只使用允许的官方 registry。
- 6 份 Python requirements lock 带 hash 且与输入文件一致。
- Desktop、Backend、Android、package-lock 和 Commitizen 版本一致。
- dependency audit 的临时例外必须有 owner、缓解措施、过期日与 regression gate。

## 4. Backend 确定性全量门禁

CI 安装：

```bash
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install --require-hashes -r backend/requirements-dev.lock
npm ci --prefix backend/app/adapters/skill/assets/ppt_ib_deck
```

执行：

```bash
cd backend
export ECHO_RUN_NODE_INSTALL=1
export ECHODESK_NODE_RUNTIME="$(command -v node)"
export ECHODESK_NODE_RUNTIME_IS_ELECTRON=true
.venv/bin/pytest tests -m "not live" \
  --junitxml=pytest-deterministic.xml \
  --cov=app --cov-report=term-missing \
  --timeout=60 --timeout-method=thread --durations=20
```

随后解析 JUnit：`failures=0`、`errors=0`、`skipped=0`。测试进程必须自然退出；打印绿色摘要后仍被非 daemon worker 卡住不算通过。

当前最终结果：

```text
916 collected
18 live deselected
898 passed
0 skipped
coverage 87%
process exited naturally
```

## 5. Backend 静态门禁

```bash
backend/.venv/bin/ruff check backend
backend/.venv/bin/ruff format --check backend
backend/.venv/bin/mypy backend/app
```

Architecture tests 额外约束：

- route snapshot 与 endpoint capability 不漂移；
- use case/port/schema 不反向依赖 adapter/FastAPI；
- durable side-effect path 使用 Workflow contract；
- local/public endpoint 和 host capability 边界明确。

## 6. 关键 Backend 反例矩阵

### Identity / public isolation

- 两个 principal 不能互读/互写 meeting、RAG、Artifact、Workflow、Agent。
- WebSocket 只能收到当前 scope event。
- token/credential 只在正确 transport 位置接受；query/header 泄漏路径被拒绝。
- renew、rotation、additional device、revoke 的 401/409/429 fail closed。
- enrollment admission、quota 和 resource ticket 不能跨 owner 重放。

对应测试集中在：

```text
test_public_principal_http.py
test_principal_repository_isolation.py
test_principal_sessions.py
test_identity_continuity.py
test_enrollment_admission.py
test_public_quota_http.py
test_transport_security.py
test_scoped_event_bus.py
```

### Workflow / Unit of Work / outbox

- domain write 成功但 run/event/outbox 失败时整体 rollback。
- revision/idempotency/active key 并发冲突。
- lease 过期、heartbeat 丢失、旧 fence 写入被拒绝。
- per-consumer、scope lane 和 global recovery 不丢消息、不永久阻塞健康 scope。
- terminal first-wins；冲突晚到 terminal 被忽略。
- cancel 与 complete 的竞态不会形成永久 Agent/Workflow 分裂。

对应测试：

```text
test_workflow_kernel.py
test_workflow_service.py
test_workflow_http_scenarios.py
test_execution_lease_store.py
test_agent_task_service.py
test_agent_bridge_recovery.py
```

### RAG / storage / upload

- 多实例共享 SQLite revision，新增/删除无需重启即可可见。
- owner manifest、content owner、quota 与 cache 一致。
- upload 超限、超时、取消和 ownership rollback。
- workspace path、symlink 和 Artifact path 不能逃逸授权根。
- SSE error/disconnect 不发送假 `done`。

对应测试：

```text
test_rag_bm25.py
test_rag_content_lifecycle.py
test_rag_sse_streaming.py
test_upload_ingress.py
test_limited_upload.py
test_workspace_scanner.py
test_ambient_storage_boundary.py
```

### Meeting / Artifact / Agent

- 同 scope 单 active meeting。
- minutes tombstone 阻止恢复重建。
- Artifact staging、metadata、link、download 同 scope。
- Agent Artifact declared/chunked oversize 与取消 cleanup。
- bridge 过期 lease 自动接管。
- Agent 成功、失败、取消、超时、retry 和 Artifact import 都有终态。

## 7. Live model contract

Live gate 只运行 provider-neutral 产品合同：

```bash
cd backend
.venv/bin/pytest tests/integration/test_product_model_live.py -m live
```

两条合同：

1. 配置的 MAIN model 非流式与流式 chat 返回指定内容并报告 usage。
2. 同一 model 生成真实 TXT Artifact，文件存在、size 匹配、内容通过断言。

缺 key、provider 不可达、timeout 或格式失败都算失败，不允许 skip。当前 GLM 结果：`2 / 2 passed`。

其它 Yunwu、STT、TTS、Web provider 诊断测试保留为独立 reachability 信息，不替代这两条产品合同。

## 8. Desktop 门禁

```bash
cd desktop
npm ci
npm run test:electron
npm run version:check
npm run lint
npm run typecheck
npm run build
CI=1 NODE_ENV=test npm run e2e
CI=1 NODE_ENV=test npm run scenarios
```

当前结果：Electron `70 passed`；E2E `95 passed`；scenarios `29 passed`。

覆盖：

- session enroll/renew/rotation/identity lost；
- WebSocket 4401/resync/reconnect/rehydrate；
- Capture/Chat SSE/TTS/meeting detail 的 timeout、cancel 和 error；
- onboarding、settings、workspace、Artifact preview、clear outputs；
- 411/960/1280/1920 responsive 与 text overflow；
- dialog/drawer accessible name、focus restore、icon label；
- update downgrade protection；
- public finalize 和 owner boundary。

## 9. Packaged 与 installed

### Packaged local smoke

验证安装包内 backend binary、版本、自定义端口、SQLite 写入、主要点击和退出后端口清理。它证明 packaging boundary，不验证真实模型和 Agent 长流程。

### Installed full workflow

真实安装 App 的完整路径必须覆盖：

1. 真实 GLM chat/RAG/minutes；
2. 故意缩短 Artifact timeout 形成失败；
3. 完整退出并重启；
4. 恢复失败状态并 retry 成功，检查 lineage 和下载；
5. 真实 AgentOS 成功与 Artifact import；
6. cancel、timeout 和 Workflow/Agent terminal 一致；
7. 最终重启后仍能恢复全部持久状态。

当前结果：`1 / 1 passed`。

## 10. Public isolation smoke

```bash
backend/.venv/bin/python scripts/public-isolation-smoke.py --self-test
backend/.venv/bin/python scripts/public-isolation-smoke.py \
  --base-url https://staged.example.invalid
```

实际 staged URL 由部署环境提供。非 loopback HTTP 必须显式 `--allow-insecure-http`，默认拒绝把 bearer 发到明文链路。

验证双 principal 的 meeting、RAG、Artifact、Workflow、Agent、WS 隔离，并在结束后撤销 session family、清理可公开删除的测试资源。输出不能打印 bearer、credential 或正文。

## 11. 跨平台发布门禁

| 平台 | 必跑 |
|---|---|
| macOS | backend binary、Electron build、codesign verify、mounted DMG smoke |
| Windows | PyInstaller backend、NSIS/zip、installed smoke、contents/hash/SBOM；未配 Authenticode时拒绝 public publish |
| Linux | x64 backend、AppImage/deb、isolated packaged smoke、hash/SBOM |
| Android / TV | development build、identity instrumentation；release 使用稳定签名并校验产物 |

任何平台“构建成功”都不能代替安装后启动、bundled backend、身份和持久化 smoke。

## 12. Agent 一致性门禁

`tests/unit/test_agent_cancel_outbox.py` 必须覆盖并持续通过：terminal HTTP read barrier、Agent/Workflow/command 同事务、远端副作用后崩溃使用同 operation key 重放、双实例只持有一个 fenced lease、first-terminal-wins 时不调用远端，以及 AgentOS `Idempotency-Key` 请求头。配套 `test_agent_task_service.py` 继续覆盖 terminal publish barrier 与成功/取消跨实例竞态。

focused 契约只用于定位失败；最终验收以本页的完整 deterministic suite、静态门禁和跨平台 CI 为准，不复制会漂移的局部测试计数。

## 13. 证据管理

- JUnit：CI artifact；源码只记录最终数字和命令。
- Playwright trace/video/screenshot：失败或 scenario artifact，不提交源码。
- 安装包、APK、SBOM、hash：release workflow artifact。
- 本机用户 DB、`.env`、credential、logs：不得进入测试证据。
- 文档更新必须区分“当前源码通过”“CI 通过”“已签名”“已发布”“已部署”。
