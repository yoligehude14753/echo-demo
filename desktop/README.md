# EchoDesk Desktop

EchoDesk v0.3.2 的跨端客户端：Electron + React 18 + TypeScript + Vite，Android / TV 使用 Capacitor 和原生身份桥接。

## 运行模式

- Electron 默认是 **local-first**：主进程启动安装包内的 backend binary，renderer 连接本机 `127.0.0.1:8769`。
- public demo 必须显式设置 `ECHO_PUBLIC_DEMO=1`；`ECHO_FORCE_LOCAL_BACKEND=1` 可以覆盖该开关回到本机模式。
- Android / TV 连接配置的 HTTPS backend，凭设备身份建立服务端 session；模型密钥不进入客户端包。
- 客户端只投影视图状态。会议、Workflow、Artifact、Agent 与 RAG 的事实源在 backend。

## Workspace 与 origin 边界

workspace 目录能力由运行时显式区分为 `local-electron`、`host-backend` 和 `unavailable`：

- local-first/self-hosted backend 继续使用服务端授权目录能力；
- public Electron 只通过 main/preload 扫描用户在本机选择的目录，再把文件发送到当前 HTTPS backend；
- public 浏览器、Android 和 TV 不回退到服务器文件系统扫描，只保留上传文档与知识库管理。

public Electron transport 要求 renderer expected origin、main-process backend、credential vault 和 session `backend_origin` 完全一致；请求固定携带 client version + bearer，只允许一次 401 renew，拒绝 3xx，并在 timeout、取消或后端切换时终止响应体读取。`workspaces.json` schema 3 按 origin 分区；同 origin mutation 串行执行，旧 origin lease 失效后不能写入新 registry，失败的 orphan 文档清理会保留并在后续扫描重试。

## 开发启动

先在仓库根目录启动 backend，再运行：

```bash
cd desktop
npm ci
npm run dev
```

需要同时调试 Electron 主进程时：

```bash
npm run electron:dev
```

## 质量门

```bash
npm run test:electron
npm run version:check
npm run lint
npm run typecheck
npm run build
CI=1 NODE_ENV=test npm run e2e
CI=1 NODE_ENV=test npm run scenarios
```

当前本地源码与受控安装态证据 [F-ECHO-028]：

- Electron main-process contracts：`177 / 177 passed`。
- Playwright mock E2E：`150 passed`。
- 业务 scenarios：`29 passed`。
- release aggregate：`31 / 31 passed`；actionlint 与 action pins 通过。
- Android / TV：current exact-SHA phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，另有 Capacitor `Hint 2`。debug APK 不可作为公开发布资产。
- desktop 与内置 `ppt_ib_deck` 的 npm audit 均为 `0` finding。

current exact-SHA macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 均通过，read-only mounted DMG smoke `1 / 1 passed`。真实安装态完整 workflow `1 / 1 passed`，覆盖下载 `0600`、marker、安全文件名、无 partial、GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart；live contract `2 / 2 passed`，`0 skipped / 0 failed`。Developer ID、notary、staple、Gatekeeper 正式链路仍为 external skipped；mock、scenario、packaged smoke 与安装态 workflow 仍是不同门禁。

## 打包

正式 macOS / Windows 入口默认 fail closed：

```bash
npm run app:dist:mac
npm run app:dist:win
npm run app:dist:linux
npm run app:build:android:development
npm run app:dist:android
npm run app:package:tv
npm run smoke:android:rotation
```

`app:dist:mac` 只接受登录钥匙串中的 `Developer ID Application` 身份和
`APPLE_KEYCHAIN_PROFILE` 指向的 notarytool 凭证。构建后会再次检查 app/DMG 的
strict codesign、Gatekeeper、notarization 和 stapled ticket：

```bash
CSC_NAME="Developer ID Application: <Name> (<TEAMID>)" \
APPLE_KEYCHAIN_PROFILE="echodesk-notary" \
npm run app:dist:mac
```

`app:dist:win` 只接受已导入 `Cert:\CurrentUser\My` 的 Authenticode 证书。发布者必须
与完整证书 Subject 相同；installer、app 和 bundled backend 都必须通过证书链与
RFC 3161 timestamp 验证：

```powershell
$env:ECHODESK_WINDOWS_CERTIFICATE_SHA1 = "<40-character-thumbprint>"
$env:ECHODESK_WINDOWS_EXPECTED_PUBLISHER = "<exact-certificate-subject>"
npm run app:dist:win
```

开发与 CI 只能使用名称明确的测试入口；这些产物不能上传为正式 Release：

```bash
npm run app:build:mac:test
npm run app:dist:mac:adhoc-test
npm run app:dist:win:unsigned-test
```

unsigned Windows workflow 只允许上传不含安装包的 smoke evidence；不得上传
`desktop/release/` 中的 EXE、blockmap、ZIP、`latest.yml`、SBOM 或 checksum。历史 run
29243474851 的 `echodesk-windows-unsigned-test` artifact 只证明 unsigned 安装态/便携 smoke 和
本地完整性合同，不能复用、重命名或上传为 v0.3.2 Release 资产。

桌面包会先生成对应平台的 PyInstaller backend，再由 electron-builder 把 binary 放入 `resources/backend/`。目标机器不需要另装 Python、Node 或运行 `install-backend.sh`。

v0.3.2 正式候选资产契约：

- macOS：DMG、DMG blockmap、ZIP、ZIP blockmap、`latest-mac.yml`、CycloneDX SBOM 和
  `SHA256SUMS-macOS.txt`，缺一项都不能进入发布审核。
- Windows：NSIS EXE、EXE blockmap、ZIP、`latest.yml`、CycloneDX SBOM 和
  `SHA256SUMS-Windows.txt`。
- Linux：`release/EchoDesk-0.3.2-linux-x86_64.AppImage`、`release/EchoDesk-0.3.2-linux-amd64.deb`。
- Android / TV：由 release 脚本输出带版本号的 APK、签名清单与 TV one-click 包。

正式 macOS / Windows 候选只从 `main` 的精确 SHA 手动触发
`.github/workflows/build-desktop-release-candidates.yml`。两个真实 hosted runner 分别通过
`desktop-release-macos` / `desktop-release-windows` 受保护 environment 获取凭证，完成签名、
安装态 smoke、hash、SBOM 和 provenance 后只上传 Actions artifact，不自动公开 Release。
macOS 会解压最终 updater ZIP，对其中 App/backend 重验 Developer ID、notary 与 Gatekeeper，
并从该 ZIP 执行 lifecycle smoke；Windows 会对 portable ZIP 内 App/backend 重验 Authenticode
后再 smoke。Updater blockmap 必须由最终 artifact 字节重算后一致。Desktop SBOM 还必须纳入
frozen backend 实际携带的 `ppt_ib_deck/package-lock.json`，不能只列 renderer 的 npm lock。
环境配置、触发、独立验签和 Android 一次性迁移清理见
[`docs/ops/formal-desktop-release.md`](../docs/ops/formal-desktop-release.md)。

`app:build:android:development` 只用于本机和 CI 验证。公开 Android / TV 资产使用
APK Signature Scheme v3.1 的 legacy→current lineage：API 24–32 继续使用历史 signer，
API 33+ 使用新的稳定 signer。发布工作流必须校验两枚证书指纹、固定的 API 33 阈值、
手机/TV 包名和 lineage；current signer 使用 Debug 或临时身份时会 fail closed。真实覆盖
安装验证见 `docs/INSTALL.md` 的 `smoke:android:rotation` 说明。

Windows 正式入口在 Authenticode、证书链或 timestamp 任一缺失时 fail closed。macOS
正式入口在 Developer ID、notarization、Gatekeeper 或 stapled ticket 任一验证失败时
fail closed；本机 ad-hoc 签名只用于验证。

正式 Windows 候选还要求预先存在受保护的 `desktop-release-windows` environment，并且四个
environment secret 全部配置；缺项必须在安装依赖和打包前明确失败，不能回退到 unsigned
构建。候选的 EXE、EXE blockmap、ZIP、`latest.yml`、SBOM、checksum 以及绑定这六个最终对象
的 GitHub provenance 必须完整，任一缺失都拒绝整套候选。服务端配置与独立核验命令见
[`docs/ops/formal-desktop-release.md`](../docs/ops/formal-desktop-release.md)。

## 关键目录

```text
desktop/
├── electron/          # main/preload、backend supervisor、credential vault
├── src/               # React UI、session transport、capture、store
├── android/           # Capacitor Android / TV 工程与身份桥接
├── scripts/           # 跨平台 backend、安装包和 smoke 脚本
└── tests/             # mock E2E、scenario、真实安装态测试
```

公开下载以 GitHub Release 实际资产为准；本地 `release/` 和 GitHub Actions artifact 都不自动代表已发布版本。
