# EchoDesk 安装指南

EchoDesk = 桌面 app（Electron + React UI）+ 内置 Python FastAPI backend（二进制打包）。

公版用户只需要安装一个桌面 app，然后输入维护者分发的访问 Key 即可使用联网能力。用户不需要安装 Python / Node / Docker，也不需要知道 yunwu 或 heyi-bj 的真实密钥。

> 注意：`https://echodesk.yoliyoli.uk/` 是 API 网关，不是网页。浏览器直接打开根路径返回 `{"detail":"Not Found"}` 是正常现象；健康检查地址是 `https://echodesk.yoliyoli.uk/health`。

---

## 公版用户安装步骤

| 项 | 要求 | 怎么装 |
|---|---|---|
| macOS | 12+ (arm64 / intel 都行) | 下载 `.dmg` 后拖入 Applications |
| Windows | Windows 10+ | 下载 `.exe` 后按安装向导执行 |
| 磁盘 | 约 1-2 GB | 安装包内含本地 backend 与声纹依赖 |
| 麦克风权限 | 首次开启后系统弹授权 | 系统设置 → 隐私与安全 → 麦克风 |

### 第 1 步：安装桌面 app

macOS：

- 下载 `EchoDesk-*.dmg`
- 打开 dmg，把 `EchoDesk.app` 拖到 `/Applications/`
- 首次打开如果提示未签名，右键 `EchoDesk.app` → 打开

Windows：

- 下载 `EchoDesk Setup *.exe`
- 双击安装
- 如 SmartScreen 提示未知发布者，确认来源后选择继续运行

### 第 2 步：输入服务网关

启动 EchoDesk 后进入「设置 → 远程服务」，填入：

```text
服务网关地址：https://echodesk.yoliyoli.uk
访问 Key：<维护者单独分发给你的 key>
```

保存后，客户端会自动把主 LLM、fast LLM、TTS、STT 指向云端 `echo-gateway`。真实 yunwu key 和 heyi-bj 内部地址只存在服务端，不会出现在客户端和公开仓库中。

### 第 3 步：开始使用

正常情况下，用户无需再做任何命令行操作：

- 本地 backend 会由 Electron 自动启动。
- 本地数据库、录音、RAG 索引保存在 `~/.echodesk/`（Windows 为 `%USERPROFILE%\.echodesk\`）。
- 联网模型、语音转文字、文字转语音通过 `https://echodesk.yoliyoli.uk` 调用。
- 没有访问 Key 或 Key 错误时，联网能力会返回 `401`。

---

## 开发者安装步骤

以下步骤只给开发者或本地源码调试使用，普通用户不需要执行。

### 源码构建桌面包

```bash
# 1) 冻结后端二进制（必须在目标平台构建）
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r packaging/requirements-build.txt
.venv/bin/pyinstaller --noconfirm packaging/echodesk-backend.spec

# 2) 打桌面安装包
cd ../desktop
npm ci
npm run app:dist        # macOS → dmg/zip
npm run app:dist:win    # Windows runner → exe
```

### 源码安装 backend（可选）

`scripts/install-backend.sh` / `scripts/install-backend.ps1` 仍保留给源码开发者使用。它会创建 `~/.echodesk/source/backend/.venv/` 并安装 Python 依赖。公版安装包已内置后端二进制，不需要跑这些脚本。

---

## 验收 checklist

正常装好的 mac 上应该满足：

- [ ] `curl http://127.0.0.1:8769/healthz` → `{"status":"ok"}`（.app 跑着时）
- [ ] `curl http://127.0.0.1:8769/healthz/full` → gateway 相关远程依赖为 `ok: true`
- [ ] `~/.echodesk/logs/backend.log` 持续 append（按天 rotate）
- [ ] `~/.echodesk/echodesk.db` 存在（启动后建空表）
- [ ] 双击 .app → UI 出现 → 顶部不出现"后端连接断开"红条
- [ ] 关掉 .app 几秒后 `lsof -ti tcp:8769` 为空（backend 跟着干净退出）
- [ ] `kill -9 $(lsof -ti tcp:8769)` 强杀 → 10s 内 UI 自动恢复（BackendSupervisor 重启）
- [ ] 设置页填入 `https://echodesk.yoliyoli.uk` + 有效访问 Key 后，联网生成 / 语音能力可用

---

## 故障排查

### "找不到 Python 3.11 或 3.12"

公版安装包正常不会依赖系统 Python。只有在内置 backend 二进制缺失、损坏，或你用源码/dev 模式时，才会回退查找系统 Python。

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

这只影响源码安装或开发者模式，不影响公版安装包。

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
- 内置 backend 二进制缺失或不可执行 → main log 应该有 `bundled-backend` / `python-not-found` 相关记录
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

### "远程依赖（网关 / Yunwu / heyi-bj）连不上"

`curl http://127.0.0.1:8769/healthz/full` 里 `remote.*.ok` 显示状态：

| 字段 | 含义 | 看哪个 |
|---|---|---|
| `echo_gateway` / gateway 相关项 | 公网服务网关 | `echo_gateway_url` 和 `echo_gateway_token` 是否正确 |
| `heyi_stt_firered` | STT，经网关代理 | 网关服务端 heyi-bj STT 是否正常 |
| `heyi_tts_qwen3` | TTS，经网关代理 | 网关服务端 heyi-bj TTS 是否正常 |
| `heyi_llm_fast` | Qwen3-1.7B，经网关代理 | 网关服务端 fast LLM 是否正常 |
| `yunwu_llm_main` | Yunwu MiniMax-M2.7，经网关代理 | 网关服务端 Yunwu key 和上游是否正常 |
| `tavily` | Tavily 搜索 | API key + 公网 |

`ok: null` + `reason: "no_api_key"` 说明访问 Key 没填，相关功能灰；`401` 说明 Key 错误或已被吊销；`ok: false` 才是真断了。

---

## 卸载

公版用户：

1. 删除 `EchoDesk.app` 或通过 Windows「应用和功能」卸载。
2. 如需清除本地数据，删除 `~/.echodesk/`（Windows 为 `%USERPROFILE%\.echodesk\`）。

源码安装用户仍可使用：

```bash
bash ~/echo-demo/scripts/install-backend.sh --uninstall
```

仅重置配置（保留数据库 / log）：删除 `~/.echodesk/config.json` 后重新启动 app。

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
└── source/                       ← 仅源码安装/dev 模式会出现
    └── backend/
        └── .venv/
```

业务数据（数据库、录音、RAG 索引、生成中间产物）默认在本地。联网模型调用会把必要请求发送到 `echo-gateway`，真实服务密钥不下发客户端。要备份本地数据：tar `~/.echodesk/` 即可（排除 `source/.venv` 没意义的大文件）。
