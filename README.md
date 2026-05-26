# Echo Demo · 数字分身 (Another Me) 演示版

> **目标**：会议 + 办公场景的"数字分身"演示 demo，按 PRD v6.7.1 实现。
> **基准 PRD**：`../echo/docs/DEMO_V1_STORYLINE.md` (v6.7.1, 2026-05-26)
> **现状**：D0 · 项目初始化中

## 架构

```
echo-demo/
├── README.md
├── ARCHITECTURE.md          # 自上而下架构图 + 模块边界
├── docs/
│   ├── PRD_v6.7.1.md       # 从 echo 仓库引入的最终 PRD
│   └── adr/                # 关键决策记录
├── backend/                # FastAPI server (Echo desktop 后端)
│   ├── app/
│   │   ├── llm/            # yoli_llm 中台 → Yunwu M2.7 主通道 + Qwen3-1.7B fast
│   │   ├── stt/            # heyi-bj FireRedASR2-AED 客户端
│   │   ├── tts/            # heyi-bj TTS 客户端
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
| LLM 主通道：**Yunwu M2.7** + Qwen3-1.7B fast | PRD §四.A.2.6 | 12.6min 会议端到端 147s |
| RAG：**jieba + BM25Okapi**（不上 mem0/LightRAG）| PRD §A.3 P1-1 | doc_cite 100% / 9 query 并发 1.28s |
| Web Search 仲裁：Inspiro 主 + Tavily 备 + DDG 兜底 | PRD §A.2 + Tavily 验证 | winner_ok 7/8 / fab_ok 6/8 |
| 一键 PPT：**pptxgenjs + Midnight 色板** | PRD §A.2.11 v6.7.1 | 417 视觉 shapes / notes 772 字/页 |
| 一键 Word：**python-docx + SKILL.md prompt** | PRD §A.2.11 v6.7.1 | 真 TOC + List style + 上标引用 |
| 一键 Excel：**openpyxl + Source 列**（去 cell.comment） | PRD §A.2.11 v6.7.1 | 4 sheet 含 DCF / 126 公式 / 46 跨 sheet / 0 errors |
| 一键 HTML：**single-file + Tailwind CDN** | PRD §A.2.11 v6.7.1 | 66K 字符 / 144 卡片块 / SVG 可视化 |
| 声纹识别：**SpeechBrain ECAPA-TDNN** 默认参数 | PRD §A.7 P2 | 本地 CPU 推理 |
| STT：**FireRedASR2-AED @ heyi-bj** | PRD §STT | 12.6min 端到端验证 |
| TTS：**heyi-bj** | PRD §TTS | — |

## 开发节奏（2 周）

详见 `docs/DEV_PLAN.md`（自上而下，按架构分层）。

## License

Proprietary
