# EchoDesk Desktop

EchoDesk v0.3.1 的跨端客户端：Electron + React 18 + TypeScript + Vite，Android / TV 使用 Capacitor 和原生身份桥接。

## 运行模式

- Electron 默认是 **local-first**：主进程启动安装包内的 backend binary，renderer 连接本机 `127.0.0.1:8769`。
- public demo 必须显式设置 `ECHO_PUBLIC_DEMO=1`；`ECHO_FORCE_LOCAL_BACKEND=1` 可以覆盖该开关回到本机模式。
- Android / TV 连接配置的 HTTPS backend，凭设备身份建立服务端 session；模型密钥不进入客户端包。
- 客户端只投影视图状态。会议、Workflow、Artifact、Agent 与 RAG 的事实源在 backend。

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

当前最终证据：

- Electron main-process contracts：`70 passed`。
- Playwright mock E2E：`95 passed`。
- 业务 scenarios：`29 passed`。
- 真实安装态 GLM + AgentOS workflow：`1 / 1 passed`。
- packaged local smoke：通过。

mock、scenario、packaged smoke 与真实安装态 workflow 是不同门禁，不能互相替代。

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

桌面包会先生成对应平台的 PyInstaller backend，再由 electron-builder 把 binary 放入 `resources/backend/`。目标机器不需要另装 Python、Node 或运行 `install-backend.sh`。

v0.3.1 预期本地构建名称：

- macOS：`release/EchoDesk-0.3.1-arm64.dmg`、`release/EchoDesk-0.3.1-arm64-mac.zip`。
- Windows：`release/EchoDesk.Setup.0.3.1.exe`、`release/EchoDesk-0.3.1-win-x64.zip`。
- Linux：`release/EchoDesk-0.3.1-linux-x64.AppImage`、`release/EchoDesk-0.3.1-linux-x64.deb`。
- Android / TV：由 release 脚本输出带版本号的 APK、签名清单与 TV one-click 包。

`app:build:android:development` 只用于本机和 CI 验证。公开 Android / TV 资产使用
APK Signature Scheme v3.1 的 legacy→current lineage：API 24–32 继续使用历史 signer，
API 33+ 使用新的稳定 signer。发布工作流必须校验两枚证书指纹、固定的 API 33 阈值、
手机/TV 包名和 lineage；current signer 使用 Debug 或临时身份时会 fail closed。真实覆盖
安装验证见 `docs/INSTALL.md` 的 `smoke:android:rotation` 说明。

Windows 正式入口在 Authenticode、证书链或 timestamp 任一缺失时 fail closed。macOS
正式入口在 Developer ID、notarization、Gatekeeper 或 stapled ticket 任一验证失败时
fail closed；本机 ad-hoc 签名只用于验证。

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
