# EchoDesk 安装指南

EchoDesk = Mac 桌面 app（Electron + React UI）+ Python FastAPI backend。
当前面向单机 / 小范围使用，**不**经 App Store 分发、**不**把 Python 打进 .app。
装机分两步：把 .app 拖到 `/Applications/`，跑一次 install 脚本。

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

优先从 GitHub Releases 下载当前 demo 包：

- macOS: `EchoDesk-0.2.1-arm64.dmg`
- Windows: `EchoDesk Setup 0.2.1.exe`
- Android: `EchoDesk-0.2.1-android-debug.apk`（内部演示 debug 包）

源码构建仅用于开发：

```bash
git clone <repo_url> ~/echo-demo
cd ~/echo-demo/desktop
npm install
```

## 第 2 步：装 .app

macOS 打开 dmg，把 `EchoDesk.app` 拖到 `/Applications/`。如果从源码构建，可用：

### 安装包产物

macOS 当前可直接构建：

```bash
cd ~/echo-demo/desktop
npm run app:dist:mac
```

产物：

```text
desktop/release/EchoDesk-0.2.1-arm64.dmg
desktop/release/EchoDesk-0.2.1-arm64-mac.zip
desktop/release/mac-arm64/EchoDesk.app
```

Windows exe 有脚本入口，但建议在 Windows 机器或带 Wine/NSIS 的构建环境跑：

```bash
cd ~/echo-demo/desktop
npm run app:dist:win
```

Android 当前用 Capacitor 打 debug APK：

```bash
cd ~/echo-demo/desktop
npm run app:dist:android
```

产物：

```text
desktop/android/app/build/outputs/apk/debug/app-debug.apk
```

APK 只是前端壳，不会在手机里启动 Electron 的本机 Python backend。模拟器默认连
`http://10.0.2.2:8769`；真机演示时需要让 Mac backend 监听局域网地址：

```bash
cd ~/echo-demo/backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8769
```

然后在 APK 里打开设置 → 移动端连接，把后端地址设成 Mac 的局域网 IP，例如
`http://10.10.12.32:8769`。生产分发建议改成 HTTPS 后端地址。

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
7. smoke test：启 backend → curl `/healthz` → kill

成功的尾巴：

```
╔══════════════════════════════════════════════════════════╗
║                  安装完成                                  ║
╚══════════════════════════════════════════════════════════╝
```

## 第 4 步：填密钥（可选）

`~/.echodesk/config.json` 里这两个 key 不填会让对应功能灰：

```json
{
  "yunwu_open_key": "sk-...",     // 不填 → @生成 / 会议纪要不可用
  "tavily_api_key": "tvly-..."    // 不填 → @查 联网检索不可用
}
```

填完不用重启 backend（user.json 在 backend 启动时读，editing 后下次重启生效；
也可以 `echo manualRestartBackend` 从 UI 触发，详见 P2 的"设置面板"）。

## 第 5 步：双击 EchoDesk.app

完事。第一次启动会：
- Electron BackendSupervisor 自动 spawn backend（PID 在 main log 可见）
- 等 `/healthz` 200 后 UI 进入工作状态
- 第一次录音时弹麦克风权限

---

## 验收 checklist

正常装好的 mac 上应该满足：

- [ ] `curl http://127.0.0.1:8769/healthz` → `{"status":"ok"}`（.app 跑着时）
- [ ] `curl http://127.0.0.1:8769/healthz/full` → 5 个远程依赖至少 3 个 `ok: true`
- [ ] `~/.echodesk/logs/backend.log` 持续 append（按天 rotate）
- [ ] `~/.echodesk/echodesk.db` 存在（启动后建空表）
- [ ] 双击 .app → UI 出现 → 顶部不出现"后端连接断开"红条
- [ ] 关掉 .app 几秒后 `lsof -ti tcp:8769` 为空（backend 跟着干净退出）
- [ ] `kill -9 $(lsof -ti tcp:8769)` 强杀 → 10s 内 UI 自动恢复（BackendSupervisor 重启）

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

主进程没成功 spawn backend。看 Electron main log：
- dev：`npm run electron-dev` 的 terminal 里
- prod：mac Console.app 搜 `EchoDesk` 或看 `~/Library/Logs/EchoDesk/main.log`

常见原因：
- Python 还是没装好 → main log 应该有 `python-not-found`
- backend log `~/.echodesk/logs/backend.log` 里有 traceback → 修对应 import 错
- 端口 8769 被别的进程占了 → `lsof -ti tcp:8769` 看是谁，kill 掉或改 `ECHO_BACKEND_PORT`

### "想跑 dev 模式但不要 .app 自动 spawn backend"

```bash
ECHO_SPAWN_BACKEND=0 npm run electron:dev
# 然后你自己开 uvicorn
cd backend && source .venv/bin/activate
python -m uvicorn app.main:app --port 8769
```

Supervisor 看到端口已占会走 external 模式，监控存活但不重启。

### "远程依赖（eight / Yunwu）连不上"

`curl http://127.0.0.1:8769/healthz/full` 里 `remote.*.ok` 显示状态：

| 字段 | 含义 | 看哪个 |
|---|---|---|
| `heyi_stt_firered` | STT @ :8090 | eight 服务 + 你的 VPN |
| `heyi_tts_qwen3` | TTS @ :8094 | 同上 |
| `heyi_llm_fast` | qwen3.5-9b-local @ :7860 | 同上 |
| `yunwu_llm_main` | Yunwu MiniMax-M2.7 | API key + 公网 |
| `tavily` | Tavily 搜索 | API key + 公网 |

`heyi_*` 是历史字段名，当前实际机器是 eight (`100.76.3.59`)。

`ok: null` + `reason: "no_api_key"` 说明 key 没填，相关功能灰；`ok: false` 才是真断了。

### "离远了声音记录不清楚 / 怀疑 STT"

先不要只看最终文字，按这几层定位：

1. 看顶部“持续监听”状态的 tooltip：新增的最近 RMS、活跃帧率、最近门控原因能区分“麦克风输入太小 / 被静音底噪门过滤 / 已进 STT 但识别差”。
2. 导出诊断包：设置 → 诊断 → 导出诊断包，里面会带 `backend.log` 当前和最近 rotated 日志，以及 `/capture/stats` 等运行状态。
3. 直接看日志：`~/.echodesk/logs/backend.log`，重点搜 `echodesk.stt`、`echodesk.workspace`、`capture`、`diagnostics`。
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

```
~/.echodesk/
├── config.json                  ← 用户配置（你可以编辑）
├── echodesk.db                  ← SQLite：meetings / ambient_segments / speakers / ...
├── logs/
│   └── backend.log              ← uvicorn / app 日志，按天 rotate，保留 14 天
├── storage/                     ← 录音 wav / meeting transcripts
├── rag_index/                   ← BM25 倒排索引
├── skill_build/                 ← @生成 HTML/PPT/Word/Excel 中间产物
└── source/
    └── backend/                 ← install-backend.sh 拷贝过来的 backend 源码副本
        └── .venv/               ← 独立 venv，Electron resolvePython 第一候选
```

所有数据 100% 在你本地，不上云。要备份：tar `~/.echodesk/` 即可（排除 `source/.venv` 没意义的大文件）。
