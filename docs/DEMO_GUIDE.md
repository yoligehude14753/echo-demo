# EchoDesk v0.3.1 Demo 复跑指南

目标：从“会议输入”走到“纪要、知识问答、办公产物和 Agent 任务”，同时验证失败可见、重启可恢复和 owner scope 不串用。

current exact-SHA 已使用 GLM 完成 live contract 与安装态完整 workflow [F-ECHO-028]；产品合同本身是 OpenAI-compatible、provider-neutral，不要求代码默认模型必须是 GLM。本地 ad-hoc 结果不能替代正式 Apple 签名链、跨平台 hosted runner 或公共环境结果。

## 1. 选择 Demo 方式

| 方式 | 用途 | 真实 backend/model | 覆盖 |
|---|---|---|---|
| Desktop mock E2E | 快速 UI 回归 | 否 | 交互、responsive、错误状态 |
| 源码本机 Demo | 人工演示 | 是 | 会议、RAG、Artifact |
| Packaged smoke | 打包验证 | 本机 backend，模型路径有限 | binary、版本、端口、持久化 |
| Installed full workflow | 发布前验收 | 是 | 模型、故障注入、重启、retry、Agent |
| Public staged Demo | 隔离验收 | staged service | session、tenant/owner、HTTP/WS 隔离 |

不要用 mock、health probe 或 packaged smoke 宣称真实业务 workflow 已通过。

## 2. 环境要求

- Python 3.11；
- Node.js 24；
- 一个可用的 OpenAI-compatible MAIN model endpoint、model 和 API key；
- Artifact Demo 需要仓库内 Node skill runtime；
- Agent Demo 需要本机 AgentOS 或 Claude Code runner；
- 麦克风 Demo 需要系统权限。

不要把 key 写进命令历史、截图或仓库。优先通过 `~/.echodesk/config.json` 或受控 `.env` 配置。

## 3. 首次安装依赖

```bash
cd <echo-repo>

python3.11 -m venv backend/.venv
backend/.venv/bin/pip install --require-hashes -r backend/requirements-dev.lock
npm ci --prefix backend/app/adapters/skill/assets/ppt_ib_deck

cd desktop
npm ci
```

先做版本与供应链检查：

```bash
cd <echo-repo>
node scripts/check-npm-lock-registries.cjs
python3 scripts/check-ci-action-pins.py
python3 scripts/check-python-locks.py
node desktop/scripts/check-version-sync.cjs
```

期望版本：`0.3.1`。

## 4. 先验证真实主模型合同

配置 MAIN model 后运行：

```bash
cd <echo-repo>/backend
.venv/bin/pytest tests/integration/test_product_model_live.py -m live
```

合同会验证：

1. 非流式与流式 chat 都返回指定内容并报告 usage；
2. 同一模型生成真实 TXT Artifact，文件内容和 size 均通过。

缺 key、timeout、返回空内容或 Artifact 不合格都算失败，不会 skip。此前本地 live 记录为 GLM `2 / 2 passed`；本轮 exact-SHA 结果以复验完成后的证据为准。

## 5. 启动源码 Demo

终端 A：

```bash
cd <echo-repo>
backend/.venv/bin/uvicorn app.main:app \
  --app-dir backend --host 127.0.0.1 --port 8769 --ws-max-size 4096
```

期望日志包含当前版本 `0.3.1`；健康检查：

```bash
curl http://127.0.0.1:8769/healthz
curl http://127.0.0.1:8769/bootstrap
```

终端 B：

```bash
cd <echo-repo>/desktop
npm run dev
```

浏览器打开 Vite 输出的本机地址。要同时验证 Electron main/preload：

```bash
npm run electron:dev
```

默认是 local-first。不要设置 `ECHO_PUBLIC_DEMO=1`，除非正在验证明确的 public staged 服务。

## 6. 人工 Demo 脚本

### A. 首次进入与工作区

1. 完成 onboarding。
2. 打开“设置 -> 工作区”，显式选择一个只含演示资料的目录。
3. 扫描后在知识库中确认文档数量变化。
4. 关闭设置，焦点回到触发位置。
5. 若使用 public Electron，切换到另一个 staged HTTPS origin 后重新打开知识库，确认旧 origin 的目录、扫描进度和文档 registry 不会出现在新 origin；旧扫描应被取消。

验收：没有内部路径溢出；知识库 dialog 与设置 drawer 的 accessible name 不冲突。public 浏览器、Android 与 TV 不显示服务器目录扫描入口，仍可上传和管理知识文档。

### B. 会议到纪要

1. 点击“开始会议”。
2. 说出至少三段包含人物、决定和待办的对话。
3. 确认转写流持续出现，状态为“正在转写”。
4. 点击“结束会议”。
5. 在 Inspector 切换到“会议纪要”，等待生成完成。

验收：历史会议出现真实段数/人数；纪要成功或显示明确失败与重试，不出现假“已完成”。

测试环境还应各执行一次生成中取消和短 timeout：会议必须离开 `generating` 并显示可重试失败；只有显式 retry 创建的新 run 可以接管生成，旧 run 的迟到结果不能覆盖新结果或已清除纪要。

### C. 知识问答

在助手中询问演示资料中的一个可引用事实。

验收：回答有 citations；只有 SSE `done` 后显示完成。中途断开或 provider error 时输入框恢复可用，并给出可执行错误。

### D. 生成产物

输入一个明确命令，例如：

```text
根据当前会议纪要生成一份两页的汇报 PPT，突出决定、负责人和下一步。
```

验收：

- 工作产物出现 running 状态；
- 成功后可预览或下载；
- Artifact 重启后仍存在；
- 失败时卡片提供重试，retry 产生新 run lineage。

### E. Agent 长任务

1. 在 Desktop Pro 中为演示 workspace 显式创建 Full Access grant。
2. 提交一个只修改演示目录的 Agent 任务。
3. 观察进度、terminal 和 Artifact import。
4. 另建任务验证取消或超时。

验收：Agent 与 Workflow terminal 最终一致，产物进入统一 outputs；public 普通模式不显示或不能调用 host-level grant/create。

### F. 重启恢复

1. 在失败 Artifact 或运行中的 durable task 后完整退出 App。
2. 重新打开。
3. 检查失败/运行状态恢复。
4. 对失败任务点击重试。
5. 再次退出并打开，确认成功结果仍在。

恢复后额外确认：同一失败任务只有一个活动 retry；清除会议纪要后，即使旧索引删除失败或旧 finalize 迟到，知识问答也不能检索到该会议。恢复 RAG 后端后，meeting/ambient 的 pending/failed 投影应按持久队列继续修复，不要求重启某个内存 BM25 实例。

## 7. Packaged 与安装态

构建 macOS 本机包：

```bash
cd <echo-repo>/desktop
npm run app:dist:mac:adhoc
npm run smoke:mac:dmg
```

ad-hoc 只用于本机验收，不等同于公开签名。

完整安装态测试入口：

```bash
cd <echo-repo>/desktop
npm run e2e:real -- --grep "installed app"
```

该测试依赖已安装 App、隔离 data dir、真实 MAIN model 和 Agent runner。运行参数以测试文件与当前环境为准，不要把本机 key 写进脚本。

current exact-SHA 结果：真实 GLM + AgentOS full workflow `1 / 1 passed`；下载文件 mode `0600`、marker、安全文件名、无 partial，以及 GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart 均已验证 [F-ECHO-028]。

## 8. Public staged Demo

Desktop public 入口必须显式设置 `ECHO_PUBLIC_DEMO=1`。部署后先运行双身份隔离 smoke：

```bash
cd <echo-repo>
backend/.venv/bin/python scripts/public-isolation-smoke.py --self-test
backend/.venv/bin/python scripts/public-isolation-smoke.py \
  --base-url <staged-https-url>
```

演示时至少使用两个独立设备身份，验证：

- A 看不到 B 的会议、RAG、Artifact、Workflow、Agent；
- A 的 WebSocket 不收到 B 的 event；
- revoke 后旧 session 不能继续连接；
- 缺失/旧客户端收到 HTTP 426、WS 4426，客户端停止 renew、业务与 WS 重连并显示升级入口；
- Electron A 后端 session 不能被发送给 B，切换 origin 后旧 UI/WS scope 被清除；
- 普通 principal 不能调用 host-admin Agent/文件能力。

另外针对 session body admission 做慢请求验证：同一 peer 并发占用 `/session` 或 `/session/enroll` body slot 时，超过 peer 上限的请求返回可重试 429；其它 peer 仍能使用剩余全局 slot，已有 bearer 的普通业务请求也不被 session body pool 阻塞。取消慢 body 或 route 完成后，slot 必须立即可再次获取。

## 9. 失败判断

以下情况必须明确记为失败或阻塞，不能降格成“基本通过”：

- pytest 打印通过但进程没有自然退出；
- deterministic suite 有 skip；
- SSE error 被 UI 显示为“已回答”；
- Agent terminal 与 Workflow 永久不一致；
- fresh create 与 retry 同时产生两个相同 `active_key` 的活动 run，或出现 child 已提交但 parent retry event/outbox 缺失；
- 纪要取消/超时/失败后永久停在 `generating`，或旧 run 覆盖新 retry/清除结果；
- 需要重启某个 RAG 内存实例才能看到新增内容；
- 清除后的 meeting 因旧 generation/物理删除失败仍可检索，或 ambient repair 重放产生重复文档；
- public 跨 principal 读写或订阅成功；
- 单一 peer 的慢 session body 占满全部多-slot pool，或该 pool 阻塞普通已认证业务；
- Electron workspace 把 A origin 的 bearer、目录 registry 或迟到扫描结果写入 B origin；
- 安装包缺 bundled backend；
- 未验证签名却写成公开可发布。

## 10. 当前门禁摘要

```text
Backend: 1045 collected / 18 live deselected / 1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors
Line coverage: 87.46% (terminal display: 87%)
Backend process: natural exit
Backend static: Ruff pass / format 250 / mypy 128 / compile pass
Electron contracts: 176 / 176
Desktop E2E: 150
Desktop scenarios: 29
Public isolation: self-test + dual-principal full smoke passed
Release aggregate: 28 / 28; actionlint + action pins passed
Android / TV current exact-SHA: phone + TV builds / JVM 4 / instrumentation 6 / APK 0.3.1 (301) / unsigned fail-closed passed
Android lint aggregate: Fatal 0 / Error 0 / Warning 0; Capacitor Hint 2; debug APK is not publishable
Dependency audit: npm 0 + 0; Python six locks valid; runtime/dev/build each retain the same controlled torch CVE-2025-3000 with no upstream fix_versions
Current exact-SHA macOS package: fresh ad-hoc arm64 DMG + ZIP / metadata + blockmap / codesign + plist + asar + forbidden scan / SBOM 1066 / SHA-256 passed
Read-only DMG smoke: 1 / 1 passed
Installed full workflow: 1 / 1 passed
Live contract: 2 / 2 passed / 0 skipped / 0 failed
Developer ID / notary / staple / Gatekeeper: external skipped
```

以上 current exact-SHA 本地结果由 [F-ECHO-028] 记录。Python `torch` 例外截至 2026-08-12，lint/typecheck/audit-tool 锁为 `0` finding，不能把 Python 总体审计写成 clean 或零漏洞。ad-hoc、unsigned 与 debug 结果均不能替代正式发布签名。

截至 2026-07-13，公共 Release / 生产 / bootstrap 仍分别为 `v0.2.50` / `0.2.49` / `0.2.45`，bootstrap 未声明 `minimum_client_version` [F-ECHO-029]。正式 signed cross-platform、受保护 environment/secret 与 public cutover 仍是外部阻塞。
