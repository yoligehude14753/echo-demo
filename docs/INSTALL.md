# EchoDesk 安装指南

当前源码版本：`v0.2.45`
公开下载页：<https://github.com/yoligehude14753/echo-demo/releases/latest>

> v0.2.45 资产需发布 workflow 生成后才会出现在 GitHub Release 中；本地开发可先用 `npm run app:build` 生成 macOS `.app`。

| 平台 | Release 资产命名 | 说明 |
|---|---|---|
| macOS Apple Silicon | `EchoDesk-0.2.45-arm64.dmg` | 桌面版安装包 |
| macOS 备用 zip | `EchoDesk-0.2.45-arm64-mac.zip` | dmg 打不开时使用 |
| Windows 安装器 | `EchoDesk.Setup.0.2.45.exe` | 普通 Windows 安装包 |
| Windows 便携包 | `EchoDesk-0.2.45-win-x64.zip` | 解压后运行 `EchoDesk.exe`；受管/远程环境优先用这个 |
| Linux AppImage | `EchoDesk-0.2.45.AppImage` | Linux x64 免安装运行 |
| Linux deb | `echodesk-desktop_0.2.45_amd64.deb` | Ubuntu / Debian 安装包 |
| Android 手机 / 平板 | `EchoDesk-0.2.45-android.apk` | 默认连接 EchoDesk 公共演示服务 |
| Android TV / 智能电视 | `EchoDesk-0.2.45-smart-tv.apk` | 适配遥控器和电视桌面入口 |
| 智能电视一键安装 | `EchoDesk-0.2.45-smart-tv-oneclick.zip` | 内含 macOS / Windows ADB 安装脚本 |
| 校验文件 | `SHA256SUMS-0.2.45.txt` | 校验下载完整性 |

EchoDesk demo 现在是多端客户端 + 公共演示服务。macOS / Windows / Linux 是桌面端；
Android / TV 是客户端壳。公开安装包默认连接 `https://echodesk.yoliyoli.uk`，
模型服务和密钥都留在服务端。

应用内也可以在「设置 → 更新」里主动检查 GitHub Release。桌面端会在启动后自动检查
新版本；用户点击「下载并安装」后才会退出覆盖安装，本机数据目录会保留。
Android / TV 因系统限制，会打开对应 APK 下载并由系统安装器确认。

私有本地部署仍支持 Electron + React UI + Python FastAPI 服务。需要本机服务时，
先跑 install 脚本，再以 `ECHO_FORCE_LOCAL_BACKEND=1` 启动桌面端。

---

## 前置要求

| 项 | 要求 | 怎么装 |
|---|---|---|
| macOS | 12+ (arm64 / intel 都行) | — |
| Python | 3.11 或 3.12 | `brew install python@3.11` 或从 [python.org](https://www.python.org/downloads/) 下安装包 |
| Node | 18+（仅 dev 期；prod 用户不需要） | `brew install node@20` |
| 磁盘 | ~3 GB（torch / speechbrain 大件） | — |
| 麦克风权限 | 首次开启后系统弹授权 | 系统设置 → 隐私与安全 → 麦克风 |

> Python 3.13 暂未实测；如果你只有 3.13，先装 3.11 兼容。

---

## 第 1 步：下载安装包

公开发布后优先从 GitHub Releases 下载当前 demo 包：

- macOS: `EchoDesk-0.2.45-arm64.dmg`
- Windows 安装器: `EchoDesk.Setup.0.2.45.exe`
- Windows 便携包（受管/远程环境推荐）: `EchoDesk-0.2.45-win-x64.zip`
- Linux: `EchoDesk-0.2.45.AppImage` 或 `echodesk-desktop_0.2.45_amd64.deb`
- Android 手机 / 平板: `EchoDesk-0.2.45-android.apk`（demo 包）
- Android TV / 智能电视: `EchoDesk-0.2.45-smart-tv.apk`
- 智能电视一键安装包：`EchoDesk-0.2.45-smart-tv-oneclick.zip`
- 电视浏览器短安装页：`https://yoligehude14753.github.io/echo-demo/`

源码构建仅用于开发：

```bash
git clone <repo_url> ~/echo-demo
cd ~/echo-demo/desktop
npm install
```

## 第 2 步：安装 / 运行客户端

macOS 打开 dmg，把 `EchoDesk.app` 拖到 `/Applications/`。如果从源码构建，可用：

### 安装包产物

macOS 当前可直接构建：

```bash
cd ~/echo-demo/desktop
npm run app:dist:mac
```

产物：

```text
desktop/release/EchoDesk-0.2.45-arm64.dmg
desktop/release/EchoDesk-0.2.45-arm64-mac.zip
desktop/release/mac-arm64/EchoDesk.app
```

Windows exe 有脚本入口，但建议在 Windows 机器或带 Wine/NSIS 的构建环境跑：

```bash
cd ~/echo-demo/desktop
npm run app:dist:win
```

如果 Windows 机器出现 Device Guard / 组织策略拦截 `.exe` 安装器，请改用
`EchoDesk-0.2.45-win-x64.zip`。解压后直接运行 `EchoDesk.exe`，本机数据仍保存在
Windows 用户目录；后续升级只需要下载新版 zip 并替换解压目录。

Linux x64 可构建 AppImage + deb：

```bash
cd ~/echo-demo/desktop
npm run app:dist:linux
```

产物：

```text
desktop/release/EchoDesk-0.2.45.AppImage
desktop/release/echodesk-desktop_0.2.45_amd64.deb
```

Android 当前用 Capacitor 打非 debuggable 的 release APK：

```bash
cd ~/echo-demo/desktop
npm run app:dist:android
npm run app:package:tv
```

产物：

```text
desktop/android/app/build/outputs/apk/release/app-release-unsigned.apk
desktop/release/EchoDesk-0.2.45-android.apk
desktop/release/EchoDesk-0.2.45-android-tv.apk
desktop/release/EchoDesk-0.2.45-smart-tv.apk
desktop/release/EchoDesk-0.2.45-smart-tv-oneclick.zip
```

Android / TV APK 是前端客户端，不会在手机或电视里启动 Electron 的本机 Python 服务。
桌面公开安装包同样默认连 `https://echodesk.yoliyoli.uk` 公共演示服务；
模型 key 留在服务端，不打进客户端包。
电视端录音优先使用原生 Android `AudioRecord`。如果某台电视系统没有把内置/遥控器麦克风
开放给三方 app，EchoDesk 会提示接入 USB / 蓝牙会议麦克风，并停止上传静音音频，避免把
硬件输入问题误判成 STT 熔断。

内网调试时，可以让 Mac 本机服务监听局域网地址：

```bash
cd ~/echo-demo/backend
source .venv/bin/activate
ECHO_LAN_FULL_API_ENABLED=true python -m uvicorn app.main:app --host 0.0.0.0 --port 8769
```

然后在 APK 里打开设置 → 移动端连接，把后端地址设成 Mac 的局域网 IP，例如
`http://10.10.12.32:8769`。
普通扫码保存会议资料不需要打开 `ECHO_LAN_FULL_API_ENABLED`；打包版桌面端会只暴露
分享页、纪要下载和产物下载这些只读保存端点。

### 智能电视安装

图里这种有「我的应用」入口的会议室电视，如果底层是 Android TV / Google TV /
国产 Android 或 AOSP TV，可以直接安装 `EchoDesk-0.2.45-smart-tv.apk`。

推荐路径：

1. 下载 `EchoDesk-0.2.45-smart-tv-oneclick.zip`。
2. 电视打开开发者模式和 ADB 网络调试。
3. 电脑和电视在同一个局域网。
4. macOS 执行 `./install-tv-macos.sh 电视IP`；Windows 执行
   `install-tv-windows.ps1 -TvIp 电视IP`。
   如果电视的 ADB 端口不是默认 `5555`，macOS 可执行
   `./install-tv-macos.sh 电视IP 5556`，Windows 可执行
   `install-tv-windows.ps1 -TvIp 电视IP -AdbPort 5556`。
   如果你希望脚本等待电视 RSA 授权后自动继续，macOS 可执行
   `ECHODESK_TV_WAIT_FOR_AUTH=1 ./install-tv-macos.sh 电视IP`，Windows 可执行
   `install-tv-windows.ps1 -TvIp 电视IP -WaitForAuth`。
5. 首次安装脚本默认清理旧 WebView / app data、授权麦克风并尝试自动打开 EchoDesk。
   升级保留数据时，运行前设置 `ECHODESK_TV_KEEP_DATA=1`。
   TV 包名是 `com.echodesk.tv`，和 Android 手机 / 平板包 `com.echodesk.app` 分离；
   默认一键安装会卸载旧 TV 遗留包 `com.echodesk.app`，避免历史数据串包。
   如需保留旧包，额外设置 `ECHODESK_TV_KEEP_LEGACY=1`。

如果脚本提示 `ADB 尚未授权`，或 `adb devices` 显示 `offline` / `unauthorized`，
说明电脑已连到电视调试端口，但电视没有接受这台电脑的 RSA 授权。请在电视上关闭再打开
「ADB 调试 / 网络调试」，看到 RSA 授权弹窗时选择允许；没有弹窗时重启电视后重试。

也可以用电视浏览器打开 `https://yoligehude14753.github.io/echo-demo/`，
遥控器选择「下载电视 APK」。
Samsung Tizen、LG webOS、Apple TV 不能安装 APK；这类设备需要外接 Android 盒子或后续浏览器/PWA 版本。

## 第 3 步：跑 install-backend.sh

```bash
bash ~/echo-demo/scripts/install-backend.sh
```

脚本会：

1. 检查 Python 3.11/3.12 是否存在
2. 创建 `~/.echodesk/` 目录结构
3. rsync backend 源码到 `~/.echodesk/source/backend/`
4. 在 `~/.echodesk/source/backend/.venv/` 建独立 venv
5. `pip install -r requirements.txt`（首次 3-10 分钟，含 torch / speechbrain）
6. 写默认 `~/.echodesk/config.json`（已存在则保留）
7. smoke test：启服务 → curl `/healthz` → kill

成功的尾巴：

```
╔══════════════════════════════════════════════════════════╗
║                  安装完成                                  ║
╚══════════════════════════════════════════════════════════╝
```

## 第 4 步：填密钥（可选）

`~/.echodesk/config.json` 里这两类 key 不填会让对应功能灰：

```json
{
  "main_model_api_key": "sk-...", // 不填 → @生成 / 会议纪要不可用
  "web_search_api_key": "..."     // 不填 → @查 联网检索不可用
}
```

填完后下次重启服务生效（`user.json` 会在服务启动时读取；也可以在 UI 里点「重启服务」）。

## 第 5 步：双击 EchoDesk.app

完事。第一次启动会：
- Electron BackendSupervisor 自动启动本机服务（PID 在 main log 可见）
- 等 `/healthz` 200 后 UI 进入工作状态
- 第一次录音时弹麦克风权限

---

## 验收 checklist

正常装好的 mac 上应该满足：

- [ ] `curl http://127.0.0.1:8769/healthz` → `{"status":"ok"}`（.app 跑着时）
- [ ] `curl http://127.0.0.1:8769/healthz/full` → 模型服务健康项为 `ok: true`
- [ ] `~/.echodesk/logs/服务日志` 持续 append（按天 rotate）
- [ ] `~/.echodesk/echodesk.db` 存在（启动后建空表）
- [ ] 双击 .app → UI 出现 → 顶部不出现"后端连接断开"红条
- [ ] 关掉 .app 几秒后 `lsof -ti tcp:8769` 为空（本机服务跟着干净退出）
- [ ] `kill -9 $(lsof -ti tcp:8769)` 强杀 → 10s 内 UI 自动恢复（BackendSupervisor 重启服务）

---

## 故障排查

### "找不到 Python 3.11 或 3.12"

脚本试了这些路径都没有：
```
/opt/homebrew/bin/python3.11
/opt/homebrew/bin/python3.12
/usr/local/bin/python3.11
/usr/local/bin/python3.12
python3.11 / python3.12 / python3 (PATH)
```

解决：装 Python 3.11，或者指定路径：
```bash
ECHO_INSTALL_PYTHON=/Users/me/.pyenv/versions/3.11.10/bin/python \
  bash scripts/install-backend.sh
```

### "pip install 失败"

最常见是网络问题。换源：
```bash
~/.echodesk/source/backend/.venv/bin/pip install \
  -r ~/.echodesk/source/backend/requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

或者 torch wheel 太大下载断了：手动单装一遍 `torch==2.4.1`。

### ".app 双击后顶部一直显示'后端连接断开'"

主进程没成功 启动本机服务。看 Electron main log：
- dev：`npm run electron-dev` 的 terminal 里
- prod：mac Console.app 搜 `EchoDesk` 或看 `~/Library/Logs/EchoDesk/main.log`

常见原因：
- Python 还是没装好 → main log 应该有 `python-not-found`
- 服务日志 `~/.echodesk/logs/服务日志` 里有 traceback → 修对应 import 错
- 端口 8769 被别的进程占了 → `lsof -ti tcp:8769` 看是谁，kill 掉或改 `ECHO_BACKEND_PORT`

### "想跑 dev 模式但不要 .app 自动 启动本机服务"

```bash
ECHO_SPAWN_BACKEND=0 npm run electron:dev
# 然后你自己开 uvicorn
cd backend && source .venv/bin/activate
python -m uvicorn app.main:app --port 8769
```

Supervisor 看到端口已占会走 external 模式，监控存活但不重启。

### "模型服务连不上"

`curl http://127.0.0.1:8769/healthz/full` 里 `remote.*.ok` 显示状态：

| 健康项 | 含义 | 看哪个 |
|---|---|---|
| 语音识别 | STT | 模型服务网络 + 权限 |
| 语音合成 | TTS | 模型服务网络 + 权限 |
| 快速智能引擎 | Fast LLM | 主模型 key + 网络 |
| 主模型 | LLM | 主模型 key + 网络 |
| 联网检索 | Web Search | 检索 key + 网络 |

`ok: null` + `reason: "no_api_key"` 说明 key 没填，相关功能灰；`ok: false` 才是真断了。

### "离远了声音记录不清楚 / 怀疑 STT"

先不要只看最终文字，按这几层定位：

1. 看顶部麦克风 / “自动记录中”状态的 tooltip：新增的最近 RMS、活跃帧率、最近门控原因能区分“麦克风输入太小 / 被静音底噪门过滤 / 已进 STT 但识别差”。
2. 导出诊断包：设置 → 诊断 → 导出诊断包，里面会带 `服务日志` 当前和最近 rotated 日志，以及 `/capture/stats` 等运行状态。
3. 直接看日志：`~/.echodesk/logs/服务日志`，重点搜 `echodesk.stt`、`echodesk.workspace`、`capture`、`diagnostics`。
4. 如需核对原始数据：本地数据库在 `~/.echodesk/echodesk.db`，录音/转写相关文件在 `~/.echodesk/storage/`。

经验判断：如果最近 RMS 长期接近 0、活跃帧率很低或最近门控是 `rms_too_low`，优先排查麦克风距离、系统输入音量、设备选择，而不是 STT 服务本身；如果 RMS/活跃帧率正常但文字质量差，再查 STT endpoint、网络和上游服务日志。

---

## 卸载

```bash
bash ~/echo-demo/scripts/install-backend.sh --uninstall
# 会要求输入 yes 确认；删 ~/.echodesk/ 整个目录（含数据库 / 录音 / 配置）
# 然后手动把 /Applications/EchoDesk.app 拖到废纸篓
```

仅重置配置（保留数据库 / log / venv）：
```bash
bash ~/echo-demo/scripts/install-backend.sh --reset-config
```

---

## 数据目录结构

下面目录只适用于私有本地后端 / `ECHO_FORCE_LOCAL_BACKEND=1` 场景。public demo 安装包
默认连接 `https://echodesk.yoliyoli.uk`，客户端本地只保存 UI 配置、设备标识和临时缓存；
模型服务、密钥和服务端会议处理都在 EchoDesk 服务端 上。

```
~/.echodesk/
├── config.json                  ← 用户配置（你可以编辑）
├── echodesk.db                  ← SQLite：meetings / ambient_segments / speakers / ...
├── logs/
│   └── 服务日志              ← uvicorn / app 日志，按天 rotate，保留 14 天
├── storage/                     ← 录音 wav / meeting transcripts
├── rag_index/                   ← BM25 倒排索引
├── skill_build/                 ← @生成 HTML/PPT/Word/Excel 中间产物
└── source/
    └── backend/                 ← install-backend.sh 拷贝过来的 backend 源码副本
        └── .venv/               ← 独立 venv，Electron resolvePython 第一候选
```

私有本地后端模式下，业务数据保存在本机 `~/.echodesk/`，要备份可 tar 这个目录
（排除 `source/.venv` 没意义的大文件）。public demo 模式不是 100% 本地数据模式。
