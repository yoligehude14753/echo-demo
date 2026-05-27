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

## 第 1 步：拿源码

EchoDesk 还没 publish 到 GitHub release，暂时通过 git 拿：

```bash
git clone <repo_url> ~/echo-demo
# 或者把已有仓库放到任意你喜欢的位置
```

## 第 2 步：装 .app

```bash
cd ~/echo-demo/desktop
npm install
npm run electron-build      # 产物在 desktop/release/
# 把 release 里的 .app 拖到 /Applications/
cp -R release/mac-arm64/EchoDesk.app /Applications/
```

> 如果你只用 dev 模式（直接 `npm run electron-dev`），可以跳过这一步。

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
ECHO_SPAWN_BACKEND=0 npm run electron-dev
# 然后你自己开 uvicorn
cd backend && source .venv/bin/activate
python -m uvicorn app.main:app --port 8769
```

Supervisor 看到端口已占会走 external 模式，监控存活但不重启。

### "远程依赖（heyi-bj / Yunwu）连不上"

`curl http://127.0.0.1:8769/healthz/full` 里 `remote.*.ok` 显示状态：

| 字段 | 含义 | 看哪个 |
|---|---|---|
| `heyi_stt_firered` | STT @ :8090 | heyi-bj 服务 + 你的 VPN |
| `heyi_tts_qwen3` | TTS @ :8094 | 同上 |
| `heyi_llm_fast` | Qwen3-1.7B @ :7860 | 同上 |
| `yunwu_llm_main` | Yunwu MiniMax-M2.7 | API key + 公网 |
| `tavily` | Tavily 搜索 | API key + 公网 |

`ok: null` + `reason: "no_api_key"` 说明 key 没填，相关功能灰；`ok: false` 才是真断了。

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
