# backend

EchoDesk 后端：FastAPI + Ports & Adapters。

## 目录

```
backend/
├── app/
│   ├── config.py             # pydantic-settings 单一配置入口
│   ├── main.py               # FastAPI 装配
│   ├── schemas/              # Pydantic 数据模型（最底层）
│   ├── ports/                # 11 个 Protocol 抽象：LLM/STT/TTS/Diarizer/RAG/Web/Skill/…
│   ├── adapters/             # 对接外部服务（Yunwu/heyi-bj/Tavily/Node skill 子进程）
│   ├── use_cases/            # 业务流程（只依赖 ports + schemas + config）
│   └── api/                  # HTTP / WebSocket 路由
└── tests/
    ├── unit/                 # 单元测试（< 100ms）
    ├── arch/                 # 架构 Fitness Function（强制单向依赖）
    ├── integration/          # 接外部服务（heyi-bj / Yunwu / Tavily）
    └── e2e/                  # 端到端含真实音频/PDF/产物
```

## 本地启动

```bash
# 1. 准备虚拟环境
cd backend
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock

# 2. 配置 .env
cp ../.env.example ../.env
# 确保 YUNWU_OPEN_KEY / TAVILY_API_KEY 已填

# 3. 启动（canonical port = 8769，与 Electron / vite / playwright 对齐）
uvicorn app.main:app --host 127.0.0.1 --port 8769

# 4. 自检
curl http://localhost:8769/healthz
curl http://localhost:8769/bootstrap
```

## 公网双身份隔离 smoke

先用独立临时目录启动 public-mode 后端，避免读写日常使用的 EchoDesk 数据：

```bash
cd /path/to/echo
RUN_DIR="$(mktemp -d /tmp/echodesk-public-smoke.XXXXXX)"
env \
  ECHO_USER_DIR="$RUN_DIR/user" \
  DB_PATH="$RUN_DIR/echo.db" \
  STORAGE_DIR="$RUN_DIR/storage" \
  RAG_INDEX_DIR="$RUN_DIR/rag" \
  SKILL_EXECUTOR_BUILD_DIR="$RUN_DIR/skills" \
  PUBLIC_DEMO_MODE=true \
  PUBLIC_HTTP_URL=http://127.0.0.1:18791 \
  PUBLIC_WS_URL=ws://127.0.0.1:18791/ws/echo \
  WORKSPACE_SCAN_ON_STARTUP=false \
  TTS_ENABLED=false \
  DIARIZER_ENABLED=false \
  WEB_SEARCH_ENABLED=false \
  AGENT_OS_ENABLED=false \
  backend/.venv/bin/uvicorn app.main:app --app-dir backend \
    --host 127.0.0.1 --port 18791
```

服务的 `/readyz` 返回 200 后，在另一个终端运行：

```bash
backend/.venv/bin/python scripts/public-isolation-smoke.py --self-test
backend/.venv/bin/python scripts/public-isolation-smoke.py \
  --base-url http://127.0.0.1:18791
```

正式公网直接传 HTTPS origin。非 loopback 的临时 HTTP 环境必须显式加
`--allow-insecure-http`，避免误把 bearer 发到明文公网链路。脚本使用纯文本 RAG
本地解析来创建 workflow，不调用 LLM，也不使用 host-admin token；输出仅含固定检查名、
HTTP 状态码、布尔值和随机 smoke id。脚本会删除 RAG 文档、清空会议 outputs、结束会议并
撤销两套 session family。会议与 workflow 的审计记录没有公开删除接口，因此会以可识别的
`isolation-<smoke-id>` 前缀保留。

## 测试

```bash
# 确定性全量门禁（不调用外部 live 服务）
pytest tests -m "not live"

# 仅快速测（unit + arch，无外部依赖）
pytest -m "unit or arch"

# 外部 live contract（单独执行，不混入确定性门禁）
pytest tests -m live

# E2E（需真实音频/PDF 素材）
pytest -m e2e
```

## 架构约束（CI 强制）

- `use_cases/` 严禁 import `adapters/` / `openai` / `anthropic` / `fastapi` / `sqlalchemy`
- `ports/` 严禁 import 任何上层（adapters/use_cases/api）或第三方 SDK（httpx/openai）
- `schemas/` 只允许依赖 pydantic + stdlib

跑 `pytest tests/arch -v` 验证。

## 运维工具

### 清空 speakers 表（spk-1..5 修复后一次性清污染）

如果你的 `~/.echodesk/echodesk.db` 是 spk-1 之前跑过的（speakers 表行数 > 30、
embedding_blob 多为 NULL），先 dry-run 看影响范围，再加 `--yes` 执行：

```bash
cd backend

# dry-run 只统计行数
python -m app.tools.reset_speakers

# 实际执行（只清 speakers）
python -m app.tools.reset_speakers --yes

# 一并清 ambient_segments + meeting_speaker_labels（保留 meetings 本身）
python -m app.tools.reset_speakers --yes --include-segments
```

下次 backend 启动时 ECAPA hydrate 读到 0 profile，计数从 1 重新开始。
详见 `docs/ARCH-AUDIT.md` §4 root #1 / #9。
