# EchoDesk · 数字分身 (Another Me)

> **目标**：会议 + 办公场景的"数字分身"桌面应用，**本地优先，数据不出机**。
> **当前版本**：v0.2.1（详见 [`CHANGELOG.md`](CHANGELOG.md)）
> **快速安装**：见 [`docs/INSTALL.md`](docs/INSTALL.md)
> **DEMO 复跑**：见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)
> **远程后端 eight endpoint（STT/TTS/Fast LLM）**：见 [`docs/REMOTE_API.md`](docs/REMOTE_API.md)

## 状态摘要 (2026-06-18)

| 阶段 | 范围 | 状态 |
|---|---|---|
| Phase 1 (0.1.0) | 持续监听 + 会议 + 9 类 intent + 一键产物 + 一键 install | ✅ released |
| Phase 2 (0.2.0) | 状态可视化 + artifact.failed + 远端降级 + DB migration + 管理 API + 诊断打包 | ✅ released |
| Phase 3 (0.2.1) | 首次启动引导 + 远端 endpoint 配置 + mac mic 权限补救 + 知识库面板 + 移动端 debug APK | ✅ demo hotfix |
| Phase 4 | Keychain 集成 + 自动更新 + Universal Binary | 计划中 |

测试：290+ unit + 9 e2e + 4 真服务 integration 全过，ruff/mypy 0 错误。

## 架构

```
echodesk/
├── README.md
├── ARCHITECTURE.md          # 自上而下架构图 + 模块边界
├── docs/
│   ├── PRD_v6.7.1.md       # 从 echo 仓库引入的最终 PRD
│   └── adr/                # 关键决策记录
├── backend/                # FastAPI server (Echo desktop 后端)
│   ├── app/
│   │   ├── llm/            # yoli_llm 中台 → Yunwu M2.7 主通道 + qwen3.5-9b-local fast
│   │   ├── stt/            # eight FireRedASR2-AED 客户端
│   │   ├── tts/            # eight TTS 客户端
│   │   ├── diarization/    # SpeechBrain ECAPA-TDNN（本地 CPU）默认参数
│   │   ├── rag/            # jieba + BM25 多文档+会议 RAG
│   │   ├── web_search/     # Inspiro 主 + Tavily 备 + 仲裁器
│   │   ├── exporters/      # Anthropic Skill v6.7.1 (PPT/Word/Excel/HTML)
│   │   ├── intent/         # 9 类 intent 路由
│   │   ├── ws/             # WebSocket broadcast (meeting state + notes)
│   │   ├── api/            # FastAPI routes
│   │   └── models/         # SQLAlchemy / pydantic schemas
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── e2e/            # 端到端场景测试
│   └── requirements.txt
├── desktop/                # Electron + React 18 + TS + Vite + Ant Design 5
│   ├── src/
│   │   ├── components/     # ChatView, NotesCard, MeetingControls, ...
│   │   ├── features/       # meeting/, document/, artifact/, agent/
│   │   ├── hooks/          # useEchoWS, useMeetingState, ...
│   │   ├── store/          # zustand
│   │   └── runtime/        # IPC + backend client
│   └── tests/
├── shared/                 # 前后端共享 types / proto
├── scripts/                # dev / e2e / demo 录屏
└── experiments_baseline/   # 从 echo 仓库迁过来的 v6.7.1 验证产物（只读参考）
```

## 已验证的技术决策（不再重测）

| 决策 | 出处 | 实测数据 |
|---|---|---|
| LLM 主通道：**Yunwu M2.7** + qwen3.5-9b-local fast | PRD §四.A.2.6 | 12.6min 会议端到端 147s |
| RAG：**jieba + BM25Okapi**（不上 mem0/LightRAG）| PRD §A.3 P1-1 | doc_cite 100% / 9 query 并发 1.28s |
| Web Search 仲裁：Inspiro 主 + Tavily 备 + DDG 兜底 | PRD §A.2 + Tavily 验证 | winner_ok 7/8 / fab_ok 6/8 |
| 一键 PPT：**pptxgenjs + Midnight 色板** | PRD §A.2.11 v6.7.1 | 417 视觉 shapes / notes 772 字/页 |
| 一键 Word：**python-docx + SKILL.md prompt** | PRD §A.2.11 v6.7.1 | 真 TOC + List style + 上标引用 |
| 一键 Excel：**openpyxl + Source 列**（去 cell.comment） | PRD §A.2.11 v6.7.1 | 4 sheet 含 DCF / 126 公式 / 46 跨 sheet / 0 errors |
| 一键 HTML：**single-file + Tailwind CDN** | PRD §A.2.11 v6.7.1 | 66K 字符 / 144 卡片块 / SVG 可视化 |
| 声纹识别：**SpeechBrain ECAPA-TDNN** 默认参数 | PRD §A.7 P2 | 本地 CPU 推理 |
| STT：**FireRedASR2-AED @ eight** | PRD §STT | 12.6min 端到端验证 |
| TTS：**eight qwen3-tts** | PRD §TTS | — |

## 开发节奏（2 周）

详见 `docs/DEV_PLAN.md`（自上而下，按架构分层）。

## License

Proprietary
