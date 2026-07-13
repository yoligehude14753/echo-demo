# EchoDesk v0.3.2 安装与运行

当前源码版本：`0.3.2`

公开下载：[GitHub Releases](https://github.com/yoligehude14753/echo-demo/releases/latest)

公开页面实际存在的文件才是已发布资产。本机构建目录和 GitHub Actions artifact 不等同于公开 Release。

## 1. 先选择运行模式

| 模式 | 适用对象 | Backend | 启用方式 |
|---|---|---|---|
| Desktop Pro | macOS / Windows / Linux 桌面用户 | 安装包内本机 backend | 默认 |
| Public demo | 明确发布的公共演示桌面入口 | HTTPS backend | `ECHO_PUBLIC_DEMO=1` |
| 强制本机 | 兼容旧 public 启动方式 | 本机 backend | `ECHO_FORCE_LOCAL_BACKEND=1` |
| Android / TV | 手机、平板、会议室电视 | HTTPS backend | 客户端配置 |

Desktop Pro 默认 local-first。Electron main process 启动 bundled backend，数据写到本机 `~/.echodesk/`；普通用户不需要安装 Python、Node 或先运行 `install-backend.sh`。

public 模式不启动本机 backend。模型密钥留在服务端，客户端使用服务端签发的设备身份和 session。

公共服务的最低客户端版本为 **0.3.2**。当前稳定版 v0.2.50 不具备设备 enrollment / session
协议，不能连接 0.3.2 公共后端；这是明确的 breaking cutover，不是向后兼容发布。公共后端
切换前必须先发布并验证可安装的 0.3.2 GitHub prerelease。渠道可以标记为 prerelease，
但二进制内的客户端版本必须自报 `0.3.2`；若自报 `0.3.2-rc.1`，会按低于稳定最低版本
fail closed。缺失、非法或低于最低版本的客户端
收到 `426 client_upgrade_required`；版本受支持但缺少 session 的请求才收到
`401 session_required`。两种响应都携带最低版本和升级地址，客户端必须停止自动重试并
进入明确的升级状态。

## 2. 从安装包安装

### macOS

预期 v0.3.2 资产名：

```text
EchoDesk-0.3.2-arm64.dmg
EchoDesk-0.3.2-arm64.dmg.blockmap
EchoDesk-0.3.2-arm64-mac.zip
EchoDesk-0.3.2-arm64-mac.zip.blockmap
latest-mac.yml
```

打开 DMG，把 `EchoDesk.app` 拖到 `/Applications`。第一次打开时授予麦克风权限。

`npm run app:dist:mac` 是正式发布入口：缺少 Developer ID、notarytool 凭证，或构建后的
codesign / Gatekeeper / stapled ticket 任一验证失败都会中止。本机和 CI 的
`app:dist:mac:adhoc-test` 只用于开发验证。DMG、ZIP、两个 blockmap 和
`latest-mac.yml` 是同一套正式 updater 资产契约，缺一项都不能发布。正式 hosted 候选还会
解压最终 updater ZIP，重新验证其中 App 与 bundled backend 的 Developer ID/Team、notary、
Gatekeeper，并直接从该 ZIP 完成安装态 lifecycle smoke；blockmap 必须能由最终 artifact
字节重新生成并逐字节匹配。

### Windows

预期资产名：

```text
EchoDesk.Setup.0.3.2.exe
EchoDesk.Setup.0.3.2.exe.blockmap
EchoDesk-0.3.2-win-x64.zip
latest.yml
EchoDesk-SBOM.cdx.json
SHA256SUMS-Windows.txt
```

正式候选还必须为上述六个最终字节对象分别提供可验证的 GitHub build provenance
attestation；provenance 不是可用空文件或自制 JSON 代替的第七个普通资产。checksum manifest
精确覆盖 EXE、EXE blockmap、ZIP、`latest.yml` 与 SBOM，候选下载后必须重新校验 hash 和全部
attestation。

常规机器使用 NSIS installer。受管环境若拦截安装器，可在可信来源和 hash 校验通过后使用 zip 便携包。

`npm run app:dist:win` 是正式发布入口：缺少 Authenticode 证书，或 installer、app、bundled
backend 的发布者、证书链、RFC 3161 timestamp 任一验证失败都会中止。Actions 的
`app:dist:win:unsigned-test` 只用于 installed smoke，不能作为正式发布资产。正式 portable
ZIP 解压后的 `EchoDesk.exe` 与 bundled backend 必须再次独立通过 Authenticode 验证后才运行
smoke，不能用 `win-unpacked` 原件的验签结果代替。

`Build Windows Installer` run 29243474851 只证明提交
`c18c98be48a1e8b54f0a30a835f7354873f7c078` 的 unsigned NSIS/ZIP、安装态/便携 smoke、SBOM
与 checksum 链可执行；它没有 Authenticode 或 provenance，且不是 `main` 正式候选。其
`echodesk-windows-unsigned-test` artifact 不得上传或重命名为 GitHub Release 资产。正式链在
`desktop-release-windows` environment、四项 Authenticode secret、签名/timestamp、完整六文件
集合或任一 provenance 缺失时必须失败关闭。

### Linux x64

预期资产名：

```text
EchoDesk-0.3.2-linux-x86_64.AppImage
EchoDesk-0.3.2-linux-amd64.deb
```

两种包都应携带 x64 backend binary。AppImage 需要先赋予执行权限；deb 使用系统包管理器安装。实际可用资产和支持发行版以 Release 说明为准。

### Android / TV

Android / TV 是远程客户端，不在设备内运行 Electron 或 Python backend。

- development APK 只用于本机、emulator 和 CI。
- 历史公开 APK 已使用 legacy signer；public APK 通过 APK Signature Scheme v3.1
  proof-of-rotation 平滑迁移，不能直接改成“只用一把新 key”。
- API 24–32 继续由 legacy signer 签名；API 33+ 由 current signer 签名，并保留旧证书历史。
- API 24–32 的签名安全性不会自动升级。legacy 私钥必须离线备份并按生产凭证保护，直到
  产品停止支持 API 32 以下；不能删除、重建或换成另一把同名 key。
- current signer 禁止 debug、自动生成或临时身份；两枚证书指纹、固定 API 33 阈值、
  phone/TV `applicationId` 或 lineage 任一不匹配都会 fail closed。
- TV 包名与手机/平板包名分离；带合法 lineage 的覆盖升级保留 app data 和设备身份。

电视侧载、ADB 授权和 one-click 包操作见 [`TV_INSTALL.md`](TV_INSTALL.md)。

## 3. 第一次启动

Desktop Pro 启动顺序：

1. Electron 计算最终 local/public mode。
2. local 模式解析 bundled backend path 和端口。
3. BackendSupervisor 启动服务并等待 `/healthz`。
4. renderer 从 main process 获取权威 endpoint，不自行根据 URL 猜模式。
5. backend 完成 migration、恢复 durable workflow/outbox/lease 后进入 ready。
6. UI bootstrap 当前会议、Workflow、Artifact 和身份状态。

默认端口为 `8769`。退出 App 后，App 自己启动的 backend 应随之退出；外部手动启动的 backend 不由 App 强杀。

## 4. 模型与工作区配置

Desktop Pro 的本机配置位于：

```text
~/.echodesk/config.json
```

可以通过应用设置页配置模型 endpoint/model/key、Web Search 和工作区。不要把 key 写入仓库、客户端包、截图或诊断包。

public / Android / TV 的模型配置属于服务端部署；客户端只保存 backend endpoint、设备身份和必要 UI 状态。

工作区目录必须由用户显式授权。Full Access Agent 可以在受信任的本机模式中使用这些目录；public 普通 principal 不获得 host filesystem 或 Agent grant。

## 5. 本机数据目录

```text
~/.echodesk/
├── config.json
├── echodesk.db
├── logs/
├── storage/
│   └── scopes/<opaque-scope>/
├── rag_index/
├── skill_build/
└── agentos/
```

目录实际创建项取决于启用功能。业务备份应在 App 退出后进行，并保护其中的会议、文件、模型配置和身份材料。

## 6. 从源码开发

要求：Python 3.11、Node.js 24。

```bash
git clone <repo-url> echo-demo
cd echo-demo

python3.11 -m venv backend/.venv
backend/.venv/bin/pip install --require-hashes -r backend/requirements-dev.lock
npm ci --prefix backend/app/adapters/skill/assets/ppt_ib_deck

backend/.venv/bin/uvicorn app.main:app \
  --app-dir backend --host 127.0.0.1 --port 8769 --ws-max-size 4096
```

另开终端：

```bash
cd echo-demo/desktop
npm ci
npm run dev
```

Electron 联调：

```bash
cd echo-demo/desktop
npm run electron:dev
```

如果要手动管理 backend：

```bash
ECHO_SPAWN_BACKEND=0 npm run electron:dev
```

然后自行启动 `uvicorn`。仅在确认端口与 data dir 隔离后这样做。

## 7. 从源码构建安装包

```bash
cd desktop
npm ci

npm run app:dist:mac
npm run app:dist:win
npm run app:dist:linux
npm run app:build:android:development
```

macOS / Windows 正式发布凭证变量见 `desktop/README.md`。开发或 CI 构建使用名称明确的
`app:build:mac:test`、`app:dist:mac:adhoc-test`、`app:dist:win:unsigned-test`，不能把
这些测试产物上传为正式 Release。

受保护 environment、精确 main SHA、真实 hosted runner 签名、安装态 smoke、完整 updater
资产、SHA-256、CycloneDX SBOM、provenance 与候选下载核验的操作步骤见
[`ops/formal-desktop-release.md`](ops/formal-desktop-release.md)。该工作流只生成候选，不会
自动公开 Release；缺凭证时会精确列出缺项并阻断，不能降级为 unsigned/ad-hoc。

Python dependency audit 在 Linux x64、macOS arm64、Windows x64 的 Python 3.11 runner 上分别
保留 raw JSON、原始 exit code 与 OS/arch 证据。runtime/dev/build 只接受文档化且仍无上游修复的
`torch` 单一非零 finding；lint/typecheck/audit-tool 必须为零 finding。三平台任一出现额外 finding、
架构漂移或把非零伪装成 clean 都会失败。Desktop SBOM 同时绑定 Python runtime、desktop npm，
以及 frozen backend 内 `ppt_ib_deck` 的 npm lock。

公开 Android / TV release 使用受控发布 workflow 和双签名输入，不应在普通开发命令中
临时生成发布身份。仓库需要配置以下 Actions Secrets（只列名称，不把值写进文档或日志）：

```text
ECHODESK_ANDROID_LEGACY_KEYSTORE_BASE64
ECHODESK_ANDROID_LEGACY_KEY_ALIAS
ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD
ECHODESK_ANDROID_LEGACY_KEY_PASSWORD
ECHODESK_ANDROID_LEGACY_CERT_SHA256
ECHODESK_ANDROID_CURRENT_KEYSTORE_BASE64
ECHODESK_ANDROID_CURRENT_KEY_ALIAS
ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD
ECHODESK_ANDROID_CURRENT_KEY_PASSWORD
ECHODESK_ANDROID_CURRENT_CERT_SHA256
```

这 10 个值只应存在于受保护 `android-release` environment。当前一次性迁移 workflow 的
执行、迁移后验证、删除 workflow 与对应 release-gates 断言，以及删除并在签发端吊销
`PUBLIC_RELEASE_TOKEN` 的完整收尾步骤见
[`ops/formal-desktop-release.md`](ops/formal-desktop-release.md#2-android-secrets-一次性迁移与清理)。

工作流固定 `ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION=33`。Gradle 只生成
`app-release-unsigned.apk`；`build-android-release.cjs` 负责生成 old→new lineage、签名并
验证 API 24–32 / API 33+、非 debuggable、zipalign、证书指纹和两个包名。公开产物包含
`EchoDesk-0.3.2-android-signing.json` 与不含私钥的
`EchoDesk-0.3.2-android-signing-lineage.bin`，并随附
`EchoDesk-0.3.2-Android-SBOM.cdx.json` 和覆盖全部 Android/TV 候选证据的
`SHA256SUMS-Android.txt`。签名 workflow 只接受 canonical repository 的 `main`，且调用方必须
传入与触发提交完全相同的 40 位 `release_sha`；命令见正式候选 SOP。

在一次性 emulator 上做真实覆盖安装验证：

```bash
export ECHODESK_ANDROID_HISTORICAL_APK=/secure/EchoDesk-0.2.34-android.apk
# 同时提供 ECHODESK_ANDROID_LEGACY_KEYSTORE / ALIAS / 两个密码 / 旧证书 SHA-256
npm run smoke:android:rotation
```

smoke 必须先观察到新 key 无 lineage 时 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，再观察到
v3.1 覆盖成功且 UID、`firstInstallTime`、麦克风授权和 `past signatures` 保持。脚本默认
拒绝物理设备和已有同包名安装，临时 current key、lineage 与对照 APK 在结束时删除。

构建前门禁：

```bash
node ../scripts/check-npm-lock-registries.cjs
python3 ../scripts/check-ci-action-pins.py
python3 ../scripts/check-python-locks.py
npm run test:electron
npm run version:check
npm run lint
npm run typecheck
npm run build
```

## 8. 安装态验收

### 通用检查

- App 显示版本 `0.3.2`，backend `/healthz` 同样返回 `0.3.2`。
- local 模式 `isPublicDemo=false`，没有显式开关时不连接 public backend。
- 创建会议后本机 SQLite 有记录，重启后仍可见。
- 退出由 App 启动的 backend 后端口释放。
- 设置、会议、知识库、Artifact 和失败重试入口可点击。

### 自动化层次

- packaged local smoke：bundled binary、端口、版本、持久化、点击。
- installed full workflow：真实 GLM、Artifact 故障注入、退出重启、retry、AgentOS、cancel、timeout 和最终恢复。
- public isolation smoke：两个 principal 的 HTTP/WS/RAG/Artifact/Workflow/Agent 负例。

current exact-SHA macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 通过，read-only DMG smoke `1 / 1 passed`；真实安装态 GLM + AgentOS 完整 workflow `1 / 1 passed`，覆盖下载 `0600`、marker、安全文件名、无 partial、GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart；live contract `2 / 2 passed`、`0 skipped / 0 failed` [F-ECHO-028]。Developer ID、notary、staple、Gatekeeper 正式链路为 external skipped；ad-hoc 结果不自动证明其它操作系统安装包或公开部署。

## 9. 旧 `install-backend.sh`

`scripts/install-backend.sh` 只保留给：

- 旧版本源码安装迁移；
- 明确需要把 backend 源码复制到 `~/.echodesk/source/backend` 的开发环境；
- 独立 backend 运维。

v0.3.2 自包含桌面包不要求运行它。不要同时让 legacy source backend 和 bundled backend 抢占同一个端口/data dir。

## 10. Public backend 验收

部署前先在独立 staged user dir/DB/storage 启动 public mode，然后运行：

```bash
backend/.venv/bin/python scripts/public-isolation-smoke.py --self-test
backend/.venv/bin/python scripts/public-isolation-smoke.py \
  --base-url <staged-https-url>
```

只有 loopback 调试允许 HTTP。非 loopback 明文 HTTP 默认被脚本拒绝，避免泄露 bearer。

切流前必须确认：

- migration 和 checksum 通过；
- 双 principal isolation smoke 通过；
- session enroll/renew/revoke 与 WS revalidate 通过；
- 客户端版本兼容；
- 旧服务和数据库快照可以 rollback。

## 11. 故障排查

### 顶栏一直显示后端断开

1. 确认没有另一个进程占用 `8769`。
2. 查看 Electron main log 和 `~/.echodesk/logs/`。
3. 确认安装包内存在 `resources/backend/echodesk-backend`（Windows 为 `.exe`）。
4. 开发模式确认 backend 与 Vite 使用同一 endpoint。

### 426 / 必须升级客户端

- 停止身份创建、续签、业务请求和 WebSocket 重连；不要把 426 当成临时网络错误。
- 使用响应里的最低版本与升级地址安装新版本，确认版本满足后再恢复连接。

### 401 / 身份丢失

- public 客户端停止普通重连，按 UI 提示重新认证或恢复设备身份。
- 不要清 local storage 后继续沿用旧 bearer，也不要静默创建新 owner。
- 409 表示身份冲突，需要用户选择恢复或明确新建身份。

### Artifact 或 Agent 卡住

- 查看对应 Workflow run 和 event，而不是只看 UI spinner。
- 检查 state、deadline、lease、outbox recovery 与 Agent bridge event。
- `cancel_requested` 长时间未收口时检查 `agent_command_outbox` 的 outcome、attempts、next_attempt_at，以及 execution lease 的 holder/fence；不要手工修改 Agent 或 Workflow 终态。

### RAG 更新后查不到

- 检查 RAG workflow terminal、`rag_documents` status 与 BM25 revision。
- 不要通过重启某个内存索引实例作为正常修复手段；SQLite manifest/revision 才是提交点。

## 12. 卸载与数据

删除应用本身不会自动删除 `~/.echodesk/`。若需要彻底清除，请先备份，再通过受控卸载/重置流程处理；不要在 App 运行时直接删除数据库或身份文件。

旧源码安装用户可以使用 `scripts/install-backend.sh --uninstall`，但它可能涉及完整用户数据目录，执行前必须阅读脚本提示并确认备份。
