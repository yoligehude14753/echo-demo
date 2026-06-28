# EchoDesk Desktop

EchoDesk 的桌面端（Electron + React 18 + TypeScript + Vite）。

当前 public demo 版本是 `v0.2.30`。正式安装包从 GitHub Release 下载：
<https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.30>

公开安装包默认连接 `https://echodesk.yoliyoli.uk` 公网 demo backend，STT/TTS/LLM
密钥不会打进客户端包。私有本机后端仍可通过 `ECHO_FORCE_LOCAL_BACKEND=1` 启用。

## 开发启动

```bash
cd desktop
npm install
npm run dev
```

## 质量门

```bash
npm run typecheck
npm run lint
npm run e2e
```

## 打包

```bash
npm run app:dist:mac
npm run app:dist:win
npm run app:dist:linux
npm run app:dist:android
npm run app:package:tv
```

主要产物：

- macOS: `release/EchoDesk-0.2.30-arm64.dmg`, `release/EchoDesk-0.2.30-arm64-mac.zip`
- Windows: `release/EchoDesk.Setup.0.2.30.exe`, `release/EchoDesk-0.2.30-win-x64.zip`
- Linux: `release/EchoDesk-0.2.30.AppImage`, `release/echodesk-desktop_0.2.30_amd64.deb`
- Android/TV: `release/EchoDesk-0.2.30-android.apk`, `release/EchoDesk-0.2.30-smart-tv.apk`

Windows 受管环境如果拦截 `.exe` 安装器，优先用 `EchoDesk-0.2.30-win-x64.zip`：
解压后直接运行 `EchoDesk.exe`，本机数据仍保存在系统用户目录，升级时替换解压目录即可。

打包后可用 `scripts/cdp-packaged-smoke.cjs` 对 Electron 产物做 CDP 点击 smoke。
