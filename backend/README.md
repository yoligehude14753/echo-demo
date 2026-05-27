# backend

EchoDesk 后端：FastAPI + Ports & Adapters。

## 目录

```
backend/
├── app/
│   ├── config.py             # pydantic-settings 单一配置入口
│   ├── main.py               # FastAPI 装配
│   ├── schemas/              # Pydantic 数据模型（最底层）
│   ├── ports/                # 8 个 Protocol 抽象：LLM/STT/TTS/Diarizer/RAG/Web/Skill/…
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
pip install -r requirements-dev.txt

# 2. 配置 .env
cp ../.env.example ../.env
# 确保 YUNWU_OPEN_KEY / TAVILY_API_KEY 已填

# 3. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload

# 4. 自检
curl http://localhost:8765/healthz
curl http://localhost:8765/bootstrap
```

## 测试

```bash
# 全部
pytest

# 仅快速测（unit + arch，无外部依赖）
pytest -m "unit or arch"

# 含集成（需 heyi-bj / Yunwu 可达）
pytest -m "unit or arch or integration"

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
