# EchoDesk 0.3 测试计划

版本：0.3.2 | 状态：当前源码契约；等待 039 功能、exact SHA 跨平台与发布证据 | 更新时间：2026-07-13

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
- migration catalog、空库建库与 038→039 真实升级都必须到 schema `039`；published v7 的 `memory_nodes` 必须继续保存在 `legacy_v7_memory_nodes` 且不得自动导入新表；migration checksum、内容漂移和 adapter 预建 fence table 路径保持 fail closed/可升级。
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
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
export ECHO_USER_DIR="$TEST_ROOT"
export DB_PATH="$TEST_ROOT/echodesk.db"
export STORAGE_DIR="$TEST_ROOT/storage"
export RAG_INDEX_DIR="$TEST_ROOT/rag_index"
export WORKSPACE_STATE_FILE="$TEST_ROOT/workspace_state.json"
export SKILL_EXECUTOR_BUILD_DIR="$TEST_ROOT/skill_build"
export ECHO_RUN_NODE_INSTALL=1
export ECHODESK_NODE_RUNTIME="$(command -v node)"
export ECHODESK_NODE_RUNTIME_IS_ELECTRON=true
.venv/bin/pytest tests -m "not live" \
  --junitxml=pytest-deterministic.xml \
  --cov=app --cov-report=term-missing \
  --timeout=60 --timeout-method=thread --durations=20
```

随后解析 JUnit：`failures=0`、`errors=0`、`skipped=0`。测试进程必须自然退出；打印绿色摘要后仍被非 daemon worker 卡住不算通过。

0.3.1 exact-SHA 历史基线（[F-ECHO-028]；0.3.2 不得继承，必须重跑）：

```text
1045 collected
18 live deselected
1027 selected
1027 passed
0 skipped
0 failed
0 errors
line coverage 87.46% (terminal display: 87%)
process exited naturally
```

## 5. Backend 静态门禁

```bash
backend/.venv/bin/ruff check backend
backend/.venv/bin/ruff format --check backend
backend/.venv/bin/mypy backend/app
```

0.3.1 exact-SHA 历史基线：Ruff check 通过；Ruff format 检查 `250 files`；mypy 检查 `128 source files`；compile 通过。0.3.2 已有的源码证据仅确认 memory 实现通过 AST/type 静态门禁 [F-ECHO-035]；本节命令仍须在最终 exact SHA 重跑。

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
- 缺失、非法、过低或超长客户端版本统一为 HTTP 426 / WS 4426；客户端进入 upgrade-required 后不再 renew、发业务请求或重连。
- Electron IPC 返回的 `backend_origin` 必须与 renderer 当前 origin 完全相等；A 的 bearer 不会到达 B。
- backend origin 切换会关闭旧 WS、丢弃 stale close、清 owner-scoped store 并以新 cursor 重新连接。
- renew、rotation、additional device、revoke 的 401/409/429 fail closed。
- enrollment admission、quota 和 resource ticket 不能跨 owner 重放。
- `/session` alias、enroll、renew 与 credential rotation 才进入 session-body pool；同 peer 慢 body 触发 peer 429 时其它 peer 和普通已认证业务仍可前进，body 取消或解析完成后 lease 必须释放。

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
- retry 的 child、parent retry event/outbox 与 domain marker 必须同事务；两个独立 WorkflowService 的 retry-vs-retry、fresh-create-vs-retry 真并发只产生一个 active-key winner，lineage 和 outbox 完整。
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
- meeting index→delete 与 delete→旧 index 两个竞态方向都由 generation fence 拒绝迟到操作；`delete_failed` 立即查询不可见且不能污染其它 owner。
- meeting/ambient due scope 合并、有界排序、失败 attempts/next-retry 和恢复成功都可重放；ambient 稳定 operation id 在“索引成功、状态未提交”后重试不重复追加。

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
- minutes run marker 只允许 owner run 写终态；取消、timeout、failure 与 Workflow terminal/event/outbox 同事务清理 generating，旧 run completion 不能覆盖显式 retry 或 clear。
- Artifact staging、metadata、link、download 同 scope。
- Agent Artifact declared/chunked oversize 与取消 cleanup。
- bridge 过期 lease 自动接管。
- Agent 成功、失败、取消、超时、retry 和 Artifact import 都有终态。

### Memory / schema 039

- 空库必须创建 `memory_nodes`、`memory_provenance`、`memory_relations`、`memory_profile_settings`、`memory_extraction_runs` 并登记 schema 039；038→039 真实升级、重跑幂等和 checksum 漂移都要覆盖。
- published v7 lineage 升级后，旧无 principal/provenance 的 `memory_nodes` 必须留在 `legacy_v7_memory_nodes`；新 owner-scoped 表为空，未经显式审查不能导入旧行。
- 两个 principal 不能互读/互写 node、provenance、relation、profile 或 extraction run；所有 API scope 只能来自认证 Principal。
- active canonical key 去重、reaffirm 计数、supersede 关系、confirm、soft delete、profile tombstone 与 revision 必须有精确断言。
- L0 working/current meeting、L1 meeting/ambient/Artifact、L2 semantic node、L3 explicit profile 的召回来源要分别覆盖；小模型 timeout/非法 JSON 时回退 deterministic ranking，不能扩大 scope 或伪造来源。
- `/memory/extract` 只能声明 `user_explicit` 来源；trusted meeting/Artifact provenance 只能由内部 ingest 写入。抽取失败/跳过必须记录 run state，不能创建虚假 active node。
- node list/get/provenance/confirm/patch/delete、profile list/put/delete、working clear 的 happy、404、输入边界和跨 owner 负例都要覆盖。

0.3.2 当前证据边界：源码与静态门禁已确认 [F-ECHO-035]，但该 fact 明确记录 functional runtime、migration、microphone、UI 和 packaged-app 尚未验证。已有 `test_cross_meeting_memory.py` 是 live RAG 跨会议召回历史测试，不替代上述 schema 039/API 回归门禁。

## 7. Live model contract

Live gate 只运行 provider-neutral 产品合同：

```bash
cd backend
.venv/bin/pytest tests/integration/test_product_model_live.py -m live
```

两条合同：

1. 配置的 MAIN model 非流式与流式 chat 返回指定内容并报告 usage。
2. 同一 model 生成真实 TXT Artifact，文件存在、size 匹配、内容通过断言。

缺 key、provider 不可达、timeout 或格式失败都算失败，不允许 skip。0.3.1 exact-SHA 历史基线为 GLM live contract `2 / 2 passed`、`0 skipped / 0 failed` [F-ECHO-028]；0.3.2 必须重跑。

GitHub 的 private-network speech/fast-model job 是可选外部门禁，只调度到同时带
`echodesk-private-models` 与 `actions-runner-2-327-1` 标签的 self-hosted runner；后一个
标签表示运维已核验 Actions Runner `>= 2.327.1`，以满足 Node 24 action runtime。没有该
runner 时 job 必须记为 skipped/blocked，不能冒充云端通过；此前产品合同由本机可达的
GLM-5.2 路径完成 `2 / 2` 实测，本轮 exact-SHA 结果以复验完成后的证据为准。

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

0.3.1 exact-SHA 历史基线：Electron `177 / 177 passed`；E2E `150 passed`；scenarios `29 passed` [F-ECHO-028]。0.3.2 必须在最终 exact SHA 重跑，并覆盖 memory source card 与管理入口。

覆盖：

- session enroll/renew/rotation/identity lost；
- WebSocket 4401/resync/reconnect/rehydrate；
- Capture/Chat SSE/TTS/meeting detail 的 timeout、cancel 和 error；
- onboarding、settings、workspace、Artifact preview、clear outputs；
- public Electron workspace 的 exact HTTPS origin/vault/session fence、3xx 拒绝、单次 401 renew、426 terminal、response-size/timeout/cancel，以及 origin-scoped registry/mutation lease/orphan retry；
- public 浏览器、Android 与 TV 的目录能力为 unavailable，不回退到服务器扫描，但上传文档管理保持可用；
- 411/960/1280/1920 responsive 与 text overflow；
- dialog/drawer accessible name、focus restore、icon label；
- update downgrade protection；
- public finalize 和 owner boundary。
- public 426 / WS 4426 熔断、升级入口、origin switch 与 Electron A→B bearer 泄漏反例。

## 9. Packaged 与 installed

### Packaged local smoke

验证安装包内 backend binary、版本、自定义端口、SQLite 写入、主要点击和退出后端口清理。Linux 分别验证 unpacked、AppImage 解包执行与真实安装 deb；Windows 分别验证 NSIS 安装态与 ZIP 解压执行。它证明 packaging boundary，不验证真实模型和 Agent 长流程。

0.3.1 exact-SHA 历史基线：macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 全部通过；read-only mounted DMG smoke `1 / 1 passed` [F-ECHO-028]。0.3.2 package 必须重跑。

### Installed full workflow

真实安装 App 的完整路径必须覆盖：

1. 真实 GLM chat/RAG/minutes；
2. 故意缩短 Artifact timeout 形成失败；
3. 完整退出并重启；
4. 恢复失败状态并 retry 成功，检查 lineage 和下载；
5. 真实 AgentOS 成功与 Artifact import；
6. cancel、timeout 和 Workflow/Agent terminal 一致；
7. 最终重启后仍能恢复全部持久状态。

0.3.1 exact-SHA 历史基线：`1 / 1 passed`。当时验证了真实下载文件 mode `0600`、marker、安全文件名、无残留 partial，以及 GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart [F-ECHO-028]。0.3.2 installed workflow 必须额外验证 039 升级后 memory 持久化、重启召回与删除/确认状态。

## 10. Public isolation smoke

```bash
backend/.venv/bin/python scripts/public-isolation-smoke.py --self-test
backend/.venv/bin/python scripts/public-isolation-smoke.py \
  --base-url https://staged.example.invalid
```

实际 staged URL 由部署环境提供。非 loopback HTTP 必须显式 `--allow-insecure-http`，默认拒绝把 bearer 发到明文链路。

验证双 principal 的 meeting、RAG、Artifact、Workflow、Agent、WS 隔离，并在结束后撤销 session family、清理可公开删除的测试资源。输出不能打印 bearer、credential 或正文。

0.3.1 exact-SHA 历史基线：self-test 与双 principal 完整 smoke 均通过 [F-ECHO-028]。0.3.2 必须加入 memory node/profile/provenance 跨 principal 负例后重跑。

## 11. 跨平台发布门禁

| 平台 | 必跑 |
|---|---|
| macOS | backend binary、Electron build、codesign verify、mounted DMG smoke |
| Windows | PyInstaller backend、NSIS/zip、installed smoke、contents/hash/SBOM；未配 Authenticode时拒绝 public publish |
| Linux | x64 backend、AppImage/deb、isolated packaged smoke、hash/SBOM |
| Android / TV | development build、identity instrumentation；release 使用稳定签名并校验产物 |

任何平台“构建成功”都不能代替安装后启动、bundled backend、身份和持久化 smoke。

Android / TV 0.3.1 exact-SHA 历史基线：phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，Capacitor `Hint 2` 单列。debug APK 不可作为公开发布资产。release aggregate `31 / 31 passed`，actionlint 与 action pins 通过 [F-ECHO-028]。0.3.2 账本分配的是 `302`，但必须以最终 APK identity gate 实测后才能写为通过。

依赖审计必须保留原始 exit code：desktop 与内置 `ppt_ib_deck` 的 npm audit 均为 `0` finding；Python six locks 均有效，runtime/dev/build 各仍报告同一项未修复且上游无 `fix_versions` 的 `torch` `CVE-2025-3000`，按 [`backend/SECURITY_DEPENDENCY_EXCEPTIONS.md`](../../backend/SECURITY_DEPENDENCY_EXCEPTIONS.md) 控制至 2026-08-12，lint/typecheck/audit-tool 为 `0`。不得把 Python 总体审计写成 clean 或零漏洞。

0.3.1 发布收口时的历史观察（2026-07-13，[F-ECHO-029]）：Developer ID、notary、staple 与 Gatekeeper 正式链路因外部签名输入缺失而 skipped；ad-hoc 结果不可替代正式签名发布。当时公共 Release / 生产 / bootstrap 分别为 `v0.2.50` / `0.2.49` / `0.2.45`，bootstrap 未声明 `minimum_client_version`。0.3.2 发布前必须重新观测公共状态；正式 signed cross-platform、受保护 environment/secret 与 public cutover 仍须以当轮证据判定。

## 12. Agent 一致性门禁

`tests/unit/test_agent_cancel_outbox.py` 必须覆盖并持续通过：terminal HTTP read barrier、Agent/Workflow/command 同事务、远端副作用后崩溃使用同 operation key 重放、双实例只持有一个 fenced lease、first-terminal-wins 时不调用远端，以及 AgentOS `Idempotency-Key` 请求头。配套 `test_agent_task_service.py` 继续覆盖 terminal publish barrier 与成功/取消跨实例竞态。

focused 契约只用于定位失败；最终验收以本页的完整 deterministic suite、静态门禁和跨平台 CI 为准，不复制会漂移的局部测试计数。

## 13. 证据管理

- JUnit：CI artifact；源码只记录最终数字和命令。
- Playwright trace/video/screenshot：失败或 scenario artifact，不提交源码。
- 安装包、APK、SBOM、hash：release workflow artifact。
- 本机用户 DB、`.env`、credential、logs：不得进入测试证据。
- 文档更新必须区分“当前源码通过”“CI 通过”“已签名”“已发布”“已部署”。
