# EchoDesk 全貌文档

> 版本：v0.2.0 · 更新时间：2026-06-01
> 定位：会议 + 办公场景的个人数字分身桌面应用，**本地优先，数据不出机**

---

## 目录

1. [产品定位](#1-产品定位)
2. [整体架构](#2-整体架构)
3. [后端架构](#3-后端架构)
   - 3.1 [分层结构](#31-分层结构)
   - 3.2 [API 端点全表](#32-api-端点全表)
   - 3.3 [Use Cases（业务编排）](#33-use-cases业务编排)
   - 3.4 [Adapters（外部依赖适配器）](#34-adapters外部依赖适配器)
   - 3.5 [数据模型与数据库](#35-数据模型与数据库)
   - 3.6 [配置字段总览](#36-配置字段总览)
4. [前端架构](#4-前端架构)
   - 4.1 [技术栈](#41-技术栈)
   - 4.2 [目录结构](#42-目录结构)
   - 4.3 [组件列表](#43-组件列表)
   - 4.4 [Hooks](#44-hooks)
   - 4.5 [全局状态（Zustand Store）](#45-全局状态zustand-store)
   - 4.6 [音频采集链路](#46-音频采集链路)
5. [Electron 集成](#5-electron-集成)
6. [核心功能点](#6-核心功能点)
   - 6.1 [Ambient 持续监听](#61-ambient-持续监听)
   - 6.2 [会议管理](#62-会议管理)
   - 6.3 [智能对话 Agent](#63-智能对话-agent)
   - 6.4 [一键产物生成（Skill）](#64-一键产物生成skill)
   - 6.5 [RAG 知识库](#65-rag-知识库)
   - 6.6 [Hybrid 向量检索](#66-hybrid-向量检索)
   - 6.7 [TTS 语音播报](#67-tts-语音播报)
   - 6.8 [语音唤醒](#68-语音唤醒)
   - 6.9 [意图路由](#69-意图路由)
   - 6.10 [说话人识别（声纹）](#610-说话人识别声纹)
   - 6.11 [工作区文档扫描](#611-工作区文档扫描)
   - 6.12 [WebSocket 实时事件总线](#612-websocket-实时事件总线)
7. [数据流详解](#7-数据流详解)
   - 7.1 [Ambient 采集流](#71-ambient-采集流)
   - 7.2 [会议全链路](#72-会议全链路)
   - 7.3 [Agent 多工具链路](#73-agent-多工具链路)
   - 7.4 [产物生成链路](#74-产物生成链路)
8. [PPT / Word / Excel / HTML 设计规范](#8-ppt--word--excel--html-设计规范)
   - 8.1 [PPT（IB 投行风 N-v3）](#81-pptib-投行风-n-v3)
   - 8.2 [Word（标书/正式文档形式）](#82-word标书正式文档形式)
   - 8.3 [Excel（自适应表结构）](#83-excel自适应表结构)
   - 8.4 [HTML（Kami warm-parchment one-pager）](#84-htmlkami-warm-parchment-one-pager)
9. [外部服务依赖](#9-外部服务依赖)
10. [测试覆盖](#10-测试覆盖)
11. [关键设计决策（ADR 摘要）](#11-关键设计决策adr-摘要)

---

## 1. 产品定位

EchoDesk 是一款运行在 macOS 的**个人数字分身**桌面应用，核心价值链：

```
持续监听环境声音
    → 自动 STT 转写 + 声纹分离
    → 构建个人知识库（RAG）
    → 会议时自动生成纪要 + 待办
    → 智能助手问答 / 联网搜索 / 自动生成 PPT/Word/Excel/HTML
    → 全程数据存本地，不上云
```

**已发布阶段**：

| 阶段 | 版本 | 核心特性 |
|---|---|---|
| Phase 1 | 0.1.0 | 持续监听 + 会议 + 9 类 intent + 一键产物 + 一键安装 |
| Phase 2 | 0.2.0 | 状态可视化 + 产物失败重试 + 远端降级 + DB 迁移 + 管理 API + 诊断打包 |
| Phase 3 | 0.2.0 | 首次启动引导 + 远端 endpoint 配置 + macOS 麦克风权限补救 + CHANGELOG/About |

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                   Desktop（Electron 28 + React 18 + Vite）          │
│                                                                     │
│  ┌───────────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────┐  │
│  │ TranscriptStream│  │  MinutesView │  │ ArtifactPanel│  │CommandBar│ │
│  │  (会议流+对话)  │  │  (纪要+Todo) │  │  (产物画廊)│  │ (指令栏) │  │
│  └───────┬────────┘  └──────┬───────┘  └─────┬─────┘  └────┬─────┘  │
│          │                  │                │             │        │
│          └──────────────────┴────────────────┴─────────────┘        │
│                          Zustand Store + useEchoWS                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  WebSocket / REST（HTTP 8769）
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Backend（FastAPI · port 8769）                  │
│                                                                     │
│  Layer 4 API ─── 16 个路由模块（health/capture/chat/rag/workspace/  │
│                  artifacts/meetings/speakers/intent/agent/tts/ws/   │
│                  admin/diagnostics）                                 │
│                                                                     │
│  Layer 3 Use Cases ─── ambient_capture · meeting_pipeline ·         │
│                        agent_loop · retrieve_and_answer ·           │
│                        generate_artifact · speak · intent_router ·  │
│                        speaker_registry · auto_meeting_detector     │
│                                                                     │
│  Layer 2 Ports ─── LLM · STT · TTS · Diarizer · RAG · Embedding ·  │
│                    WebSearch · Skill · Repository · EventBus ·      │
│                    Intent · Punctuator                              │
│                                                                     │
│  Layer 1 Adapters ─── openai_compatible · firered · qwen3_tts ·     │
│                       ecapa · bm25/hybrid/vector_store · bge_m3 ·   │
│                       llm_skill · tavily · sqlite · inmemory_bus    │
│                                                                     │
│  Layer 0 Infra ─── pydantic-settings · structlog · SQLite           │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        External Services                            │
│  Yunwu M2.7（主 LLM）  │  heyi-bj（STT/TTS/Qwen3-1.7B fast）      │
│  Tavily + DDG（Web 搜索）  │  本地 SpeechBrain ECAPA（声纹）        │
└─────────────────────────────────────────────────────────────────────┘
```

**架构原则**：

- `use_cases` 只依赖 `ports` 接口，**禁止** import `adapters`（CI Fitness Function 校验）
- `api` 只 import `use_cases` 和 `schemas`
- 外部 I/O 全在 `adapters` 层，可独立替换，不影响业务逻辑

---

## 3. 后端架构

### 3.1 分层结构

```
backend/app/
├── main.py              ← FastAPI 入口 + lifespan（单例依赖注入）
├── config.py            ← pydantic-settings Settings（所有配置集中）
├── config_io.py         ← ~/.echodesk/config.json 运行时读写
├── api/                 ← 16 个路由模块
├── use_cases/           ← 11 个业务编排模块
├── schemas/             ← 11 个 Pydantic 数据模型
├── ports/               ← 12 个 Port 接口（协议定义）
└── adapters/
    ├── llm/openai_compatible.py
    ├── stt/firered.py + llm_punctuator.py
    ├── tts/qwen3_tts.py
    ├── diarizer/ecapa.py
    ├── rag/bm25.py + hybrid.py + vector_store.py + factory.py + parsers.py + workspace_scanner.py
    ├── embedding/bge_m3_local.py + yunwu.py + router.py
    ├── skill/llm_skill.py + python_executor.py + node_executor.py + prompts.py + assets/
    ├── intent/llm_router.py
    ├── web_search/tavily.py
    ├── repo/sqlite.py + migrator.py + migrations/*.sql
    ├── event_bus/inmemory.py
    └── audio_gate.py + audio/wav.py
```

### 3.2 API 端点全表

#### Meta / Health

| Method | Path | 功能 |
|---|---|---|
| GET | `/healthz` | 轻量存活检查 `{"status":"ok"}` |
| GET | `/healthz/full` | 扩展健康：DB + LLM/STT/TTS TCP 探针 |
| GET | `/bootstrap` | 前端启动配置（WS/HTTP URL、功能开关） |

#### Ambient 采集

| Method | Path | 功能 |
|---|---|---|
| POST | `/capture/chunk` | 上传 PCM chunk → STT + RAG（ambient 主链路） |
| GET | `/capture/stats` | 采集链路统计 |
| GET | `/capture/recent` | 最近 ambient 转录文本 |

#### 智能问答

| Method | Path | 功能 |
|---|---|---|
| POST | `/chat` | 通用流式问答 SSE（支持 MAIN/FAST/具体 model 名） |
| POST | `/rag/ask` | RAG + 可选 Web 联网检索式问答 SSE |
| POST | `/agent/run` | 多工具 Agent 循环 SSE（rag→web→产物→final） |

#### RAG / 知识库

| Method | Path | 功能 |
|---|---|---|
| POST | `/rag/ingest` | multipart 上传文档（PDF/docx/xlsx/html/csv/txt）入库 |
| GET | `/rag/stats` | 索引诊断（chunk/doc 数量） |
| GET | `/rag/docs` | 已入库文档列表 |
| DELETE | `/rag/docs/{doc_id}` | 删除文档 |

#### 工作区扫描

| Method | Path | 功能 |
|---|---|---|
| GET | `/workspace/status` | 工作区配置与扫描状态 |
| POST | `/workspace/scan` | 手动触发目录增量扫描 |
| POST | `/workspace/add-dir` | 添加可索引目录 |
| POST | `/workspace/remove-dir` | 移除目录 |
| POST | `/workspace/clear` | 清空工作区索引 |

#### 产物生成

| Method | Path | 功能 |
|---|---|---|
| GET | `/artifacts` | 历史产物列表 |
| POST | `/artifacts/generate` | 同步生成产物（阻塞直到完成） |
| POST | `/artifacts/generate/stream` | 流式生成 + 进度 SSE |
| GET | `/artifacts/{artifact_id}/download` | 下载产物文件 |

#### 会议管理

| Method | Path | 功能 |
|---|---|---|
| GET | `/meetings/current` | 当前活跃会议状态 |
| POST | `/meetings/manual_start` | 手动开始会议 |
| POST | `/meetings/manual_end` | 手动结束会议 |
| GET | `/meetings` | 历史会议列表 |
| GET | `/meetings/{id}/transcript` | 转写段列表 |
| GET | `/meetings/{id}/minutes` | 会议纪要 JSON |
| POST | `/meetings/{id}/finalize` | 触发纪要生成 |
| POST | `/meetings/{id}/end` | 结束会议 |
| POST | `/meetings/{id}/inject_segment` | 注入测试转写段 |

#### 说话人

| Method | Path | 功能 |
|---|---|---|
| GET | `/speakers` | 已知说话人列表（含用户改名） |
| POST | `/speakers/{speaker_id}/rename` | 用户改名（持久化） |

#### TTS

| Method | Path | 功能 |
|---|---|---|
| POST | `/tts/speak` | 合成 PCM 16kHz mono，返回二进制 |
| POST | `/tts/suggest` | 只推 WS 事件，不实际合成 |
| GET | `/tts/diag` | 真实合成探针（StatusBar 健康检查用） |

#### 意图路由

| Method | Path | 功能 |
|---|---|---|
| POST | `/intent/route` | 9 类意图分类，返回 `IntentResult` |

#### WebSocket

| Method | Path | 功能 |
|---|---|---|
| WS | `/ws/echo` | 实时事件总线（会议/产物/纪要/TTS/ping） |

#### 管理 API（前缀 `/admin`）

| Method | Path | 功能 |
|---|---|---|
| GET | `/admin/data-dir` | 数据目录占用 |
| POST | `/admin/meetings/{id}/export` | 导出会议 zip |
| POST | `/admin/speakers/reset` | 重置说话人 |
| GET | `/admin/settings/remote` | 查看远端 endpoint 配置（脱敏） |
| PATCH | `/admin/settings/remote` | 修改 LLM/STT/TTS/Tavily 配置 |
| GET | `/admin/diagnostics/export` | 导出诊断包 zip |

### 3.3 Use Cases（业务编排）

| 模块 | 职责 |
|---|---|
| `ambient_capture.py` | Ambient chunk：落盘 → STT → VAD 门控 → RAG ingest |
| `auto_meeting_detector.py` | 音量/转写自动触发开会/结会状态机 |
| `meeting_state.py` | 全局 idle/in_meeting 单例状态机 |
| `meeting_pipeline.py` | 会议 chunk：STT → 声纹分离 → 转写段入库 → 纪要生成 → RAG |
| `agent_loop.py` | 主 LLM 多步工具循环（rag_search / web_search / generate_artifact / final_answer） |
| `retrieve_and_answer.py` | RAG-grounded 问答（Fast 分类 + diverse rerank + Grep boost） |
| `ask_question.py` | 通用流式问答（直连主 LLM） |
| `generate_artifact.py` | 调 SkillExecutor 生成产物文件（流式进度） |
| `intent_router.py` | 调 IntentRouterPort，返回 9 类 intent |
| `speaker_registry.py` | 说话人编号 + label 管理（用户改名持久化） |
| `speak.py` | TTS 主链路：文本 → PCM → WS 推流 → 前端播放 |

### 3.4 Adapters（外部依赖适配器）

| 子目录 | 关键文件 | 说明 |
|---|---|---|
| `llm/` | `openai_compatible.py` | OpenAI 兼容协议客户端，支持 Yunwu/heyi 双通道，带 `_ThinkStripper` 去除思维链 |
| `stt/` | `firered.py` | heyi-bj FireRedASR2-AED HTTP 客户端；`llm_punctuator.py` 用 Qwen3-1.7B 补标点 |
| `tts/` | `qwen3_tts.py` | heyi-bj faster-qwen3-tts，PCM 16kHz mono 输出 |
| `diarizer/` | `ecapa.py` | 本地 SpeechBrain ECAPA-TDNN 声纹识别，纯 CPU |
| `rag/` | `bm25.py` | jieba 分词 + BM25Okapi，索引存 `~/.echodesk/rag_index/` |
| `rag/` | `hybrid.py` | BM25 + dense 向量 RRF 融合（k=60） |
| `rag/` | `vector_store.py` | hnswlib cosine 向量索引 + sidecar JSON |
| `rag/` | `factory.py` | `embedding_enabled=True` → `HybridRag`；否则 → `BM25Rag` |
| `rag/` | `parsers.py` | markitdown 万能文档解析（PDF/docx/pptx/xlsx/html/csv） |
| `rag/` | `workspace_scanner.py` | 授权目录增量扫描 ingest |
| `embedding/` | `router.py` | BgeM3Local（优先）→ YunwuOpenAI（兜底）动态路由 |
| `skill/` | `llm_skill.py` | 产物 Skill 核心：路由→LLM→执行→修复重试→流式进度 |
| `skill/` | `python_executor.py` | 沙箱执行 LLM 生成的 Python 代码（Word/Excel/PDF） |
| `skill/` | `node_executor.py` | Node.js 子进程执行 pptxgenjs 渲染脚本 |
| `skill/` | `prompts.py` | 所有 Skill 的系统提示（体裁自适应） |
| `skill/assets/ppt_ib_deck/` | `ib_master.pptx` + `render.mjs` + `schema.md` | IB 母版 + docxtemplater 渲染器 + JSON Schema |
| `intent/` | `llm_router.py` | Qwen3-1.7B 快速 9 类意图分类 |
| `web_search/` | `tavily.py` | Tavily 主 + DDG 兜底联网搜索 |
| `repo/` | `sqlite.py` | SQLite 异步仓库（meeting/segment/speaker/minutes） |
| `event_bus/` | `inmemory.py` | 内存 WS 事件总线，支持 `last_seq` 重放去重 |
| 根级 | `audio_gate.py` | RMS + VAD 音频门控（过滤静音/低信噪比） |

### 3.5 数据模型与数据库

**SQLite 表**（`~/.echodesk/echodesk.db`）：

| 表名 | 说明 |
|---|---|
| `meetings` | 会议记录（id, title, status, minutes_status, display_title, 时间戳） |
| `meeting_segments` | 转写段（meeting_id, speaker_id, text, ts_start, ts_end） |
| `meeting_speaker_labels` | 会议内说话人 label（meeting_id, speaker_id, label） |
| `ambient_segments` | Ambient 转写段（text, ts, source） |
| `speakers` | 全局说话人（id, embedding, label, label_user_set） |
| `schema_version` | DB 迁移版本追踪 |

**5 次 SQL 迁移**（`adapters/repo/migrations/`）：

| 文件 | 变更 |
|---|---|
| `001_initial.sql` | 建表基线 |
| `002_schema_version.sql` | 增加版本追踪表 |
| `003_minutes_status.sql` | `meetings.minutes_status` + `minutes_error` + 历史回填 |
| `004_minutes_todos.sql` | `meetings.display_title` |
| `005_speaker_label_user_set.sql` | `speakers.label_user_set` |

**Pydantic Schemas**（`app/schemas/`）：

| 文件 | 主要类型 |
|---|---|
| `agent.py` | `ToolResult`, `AgentEvent`（tool_call/tool_result/artifact/delta/final/error/done） |
| `artifact.py` | `ArtifactRequest`, `GeneratedArtifact`, `SUPPORTED_KINDS`（html/pptx/word/xlsx/pdf/txt/markdown） |
| `events.py` | `EchoEvent`（WS 协议），`EventType` 枚举，`ClientHello`/`ServerHello` |
| `meeting.py` | `TranscriptSegment`, `TodoItem`, `MeetingMinutes`, `MeetingSummary`, `MeetingCard` |
| `llm.py` | `ChatMessage`, `LLMUsage`, `LLMResponse` |
| `rag.py` | `RagChunk`, `WebHit`, `RetrievalResult` |
| `skill_progress.py` | `SkillProgress`（SSE 进度帧），`SkillProgressEnvelope` |

### 3.6 配置字段总览

**LLM**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `llm_main_provider` | `"yunwu"` | 主通道提供商 |
| `llm_main_model` | `"MiniMax-M2.7"` | 主 LLM |
| `llm_main_base_url` | — | Yunwu API 地址 |
| `llm_fast_provider` | `"heyi-local"` | 快速通道（意图路由/补标点） |
| `llm_fast_model` | `"Qwen3-1.7B"` | 本地快速模型 |
| `llm_main_max_tokens` | `80000` | 主 LLM 最大 token |
| `llm_fast_max_tokens` | `512` | 快速模型最大 token |

**STT**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `stt_backend` | `"firered"` | STT 后端 |
| `stt_firered_url` | — | FireRedASR2-AED 地址 |
| `stt_language` | `"zh"` | 识别语言 |
| `ambient_llm_punctuate` | `True` | 是否用 LLM 补标点 |
| `ambient_punctuator_timeout_s` | `2.0` | 补标点超时 |

**TTS**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `tts_enabled` | `True` | 是否启用 TTS |
| `tts_provider` | `"qwen3_tts"` | TTS 后端 |
| `tts_qwen3_url` | — | heyi-bj TTS 地址 |
| `tts_qwen3_voice` | `"aiden"` | 音色 |

**声纹 / 音频门控**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `diarizer_enabled` | `True` | 是否启用声纹分离 |
| `diarizer_backend` | `"ecapa"` | 声纹识别后端 |
| `diarizer_match_threshold` | `0.55` | 余弦相似度阈值 |
| `ambient_rms_gate` | `800` | RMS 静音门控阈值 |
| `ambient_min_speech_frame_ratio` | `0.15` | 最低有效语音帧占比 |
| `ambient_max_cps` | `10.0` | 字/秒上限（防幻听） |

**RAG / Embedding**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `rag_index_dir` | `~/.echodesk/rag_index` | BM25 索引目录 |
| `rag_top_k` | `1000` | 初始召回数 |
| `rag_pdf_chunk_tokens` | `600` | PDF 切片 token 数 |
| `embedding_enabled` | `False` | 是否启用 dense 向量检索 |
| `embedding_main_provider` | `"bge_m3_local"` | 向量模型（本地优先） |
| `embedding_fallback_provider` | `"yunwu"` | 云端向量兜底 |

**Skill**：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `skill_executor_build_dir` | `~/.echodesk/skill_build` | 产物构建目录 |
| `skill_executor_max_tokens` | `12000` | Skill LLM 最大 token |
| `use_legacy_html_pptx` | `False` | 是否回退旧版 HTML/PPT skill |

---

## 4. 前端架构

### 4.1 技术栈

| 技术 | 版本/说明 |
|---|---|
| Electron | 28，`contextIsolation: true`，preload IPC |
| React | 18，函数组件 + hooks |
| TypeScript | 全量 strict |
| Vite | 构建 + dev 热更新 |
| Zustand | 全局状态管理（单文件 `store.ts`） |
| Tailwind CSS | 样式（推测，index.css 中引入） |
| Playwright | E2E 测试 |

### 4.2 目录结构

```
desktop/src/
├── main.tsx             ← React 入口
├── App.tsx              ← 根组件（布局骨架）
├── api.ts               ← 后端 HTTP 客户端封装
├── ws.ts                ← WebSocket 客户端 + useEchoWS hook
├── store.ts             ← Zustand 全局 store
├── types.ts             ← 全局类型定义
├── runtime.ts           ← Electron IPC / 浏览器环境桥接
├── components/          ← 14 个 UI 组件
├── hooks/               ← 5 个业务 hooks
├── capture/             ← 音频采集模块
│   ├── useEchoCapture.ts
│   ├── audioCapture.ts
│   ├── captureChunkRouter.ts   ← Ambient/Meeting 路由分发
│   └── pcm.ts
├── lib/
│   ├── voiceWake.ts     ← 语音唤醒词检测（WAKE_RE 正则）
│   ├── failedArtifact.ts
│   ├── explicitArtifactCommand.ts
│   └── speakerDisplay.ts
└── domain/session.ts
```

### 4.3 组件列表

| 组件 | 功能 |
|---|---|
| `CommandBar.tsx` | 底部指令输入框（chat / rag / agent / 产物生成路由） |
| `TranscriptStream.tsx` | 实时转写流 + 人机对话流（会话气泡） |
| `MinutesView.tsx` | 会议纪要展示 + Todo 列表 + 执行操作 |
| `ArtifactPanel.tsx` | 产物列表面板（下载/预览/失败重试） |
| `ArtifactPreviewModal.tsx` | 产物预览弹窗 |
| `MeetingList.tsx` | 左侧历史会议列表（`aria-current` 标记活跃项） |
| `MeetingStatusBar.tsx` | 顶栏：当前会议状态 + 时长 + 操作按钮 |
| `StatusBar.tsx` | 底栏：后端连接 / TTS / STT / 探针健康 pill |
| `WorkspaceBar.tsx` | 工作区 / RAG 文档数量状态条 |
| `SettingsPanel.tsx` | 设置面板（远端 endpoint / 工作区目录） |
| `OnboardingModal.tsx` | 首次启动引导弹窗 |
| `AboutModal.tsx` | 关于/版本/更新日志弹窗 |
| `CitationText.tsx` | RAG 答案引用角标渲染 |
| `CaptureStatus.tsx` | 采集状态组件（当前 App 中未渲染） |

### 4.4 Hooks

| Hook | 功能 |
|---|---|
| `useBackendHealth.ts` | 每 2s 轮询 `/healthz`，驱动 StatusBar |
| `useMeetingHistory.ts` | 会议列表 hydrate + 会议详情懒加载 |
| `useTtsPlayer.ts` | TTS PCM 播放器 + `/tts/diag` 健康探针 |
| `useVoiceWakeAgent.ts` | STT 转写结果 → 唤醒词检测 → 触发 Agent |
| `useOnboarding.ts` | 首次启动逻辑（localStorage 标记） |
| `capture/useEchoCapture.ts` | 麦克风采集主 hook（VAD + 分块 + 路由） |

### 4.5 全局状态（Zustand Store）

**核心 State**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `currentMeetingId` | `string \| null` | 当前选中会议 |
| `meetings` | `Record<string, MeetingCard>` | 会议缓存（WS 实时更新） |
| `conversationEvents` | `ConversationEvent[]` | 人机会话气泡（user_command / assistant_reply / rag_answer） |
| `artifacts` | `GeneratedArtifact[]` | 产物列表 |
| `failedArtifacts` | `FailedArtifact[]` | 失败产物（可重试） |
| `pendingArtifactBriefs` | `Map` | 生成中的产物 brief 映射 |
| `connected` | `boolean` | WS 连接状态 |
| `meetingHistoryResyncNonce` | `number` | 强制触发历史列表刷新 |

**主要 Actions**：

- 会议：`selectMeeting`, `hydrateMeetings`, `upsertMeeting`, `applyEvent`（处理 WS 事件派发）
- 产物：`addArtifact`, `removeArtifact`, `dismissFailedArtifact`
- 会话：`appendUserCommand`, `appendAssistantReply`, `patchAssistantReply`, `clearConversationEvents`
- CommandBar 预填：`registerCommandBarPrefill`, `prefillCommandBar`
- 其它：`setConnected`, `requestMeetingHistoryResync`, `reset`

### 4.6 音频采集链路

```
麦克风（MediaStream）
  └─ audioCapture.ts（PCM 分帧，128ms/帧）
      └─ captureChunkRouter.ts（唯一写入点）
          ├─ 永远 → POST /capture/chunk（ambient 主链路）
          │         ├─ 落盘 ~/.echodesk/ambient/YYYY-MM-DD/
          │         ├─ STT → ambient RAG ingest
          │         └─ 若有活跃会议 → MeetingPipeline 叠加
          └─ lib/voiceWake.ts → 检测唤醒词 → useVoiceWakeAgent
```

**唤醒词检测**（`lib/voiceWake.ts`）：

支持变体：`echo`, `echodesk`, `aiko`, `pico`, `诶口`, `欸口`, `艾口` 等，配合前置语句边界锚定，防误触。函数：`extractEchoWakeCommand(raw)` → 提取唤醒后的指令文本。

---

## 5. Electron 集成

**进程架构**：

```
Electron Main Process（main.cjs）
├── 启动 backend（Python uvicorn，端口 8769）
├── 健康监控（每 2s /healthz，3 次失败重启，backoff 1s/3s/10s）
├── 日志管理（~/.echodesk/logs/runtime.log，8MB 滚动）
└── IPC Handlers
    ├── echo:backend-host       → 返回 http://127.0.0.1:8769
    ├── backend:manual-restart  → 手动重启 backend
    ├── mic:status/request      → macOS 麦克风权限
    ├── echo:open-artifact-in-system → shell.openPath
    └── workspace:pick-directory    → 目录选择对话框

Electron Renderer Process（React 18 in Chromium）
└── preload.cjs（contextBridge 暴露安全 IPC）
```

**Python 解释器候选顺序**：

1. `ECHO_PYTHON` 环境变量
2. `~/.echodesk/source/backend/.venv/bin/python`（运行时 venv）
3. 项目 `backend/.venv/bin/python`（开发 venv）
4. `/usr/bin/python3`

**窗口配置**：

- 尺寸：1280×820，最小 960×600
- `titleBarStyle: "hiddenInset"`（macOS 原生风格）
- 生产环境加载 `dist/index.html`；开发环境 `http://localhost:5173`
- `nodeIntegration: false`，`contextIsolation: true`

---

## 6. 核心功能点

### 6.1 Ambient 持续监听

- App 启动即开始，无需手动控制，24 小时持续
- 每 128ms 一帧 PCM → VAD 门控（RMS < 800 丢弃）→ STT → 非空则 RAG ingest
- 数据标签 `source=ambient`，与会议数据共享同一 RAG 索引
- 落盘：`~/.echodesk/ambient/YYYY-MM-DD/*.wav`
- 意义：个人数字分身的环境声音记忆层，问答时可检索到会议外的对话内容

### 6.2 会议管理

- 支持**手动**（CommandBar `@开始会议`）和**自动**（AutoMeetingDetector 音量触发）两种模式
- 会议中每帧音频同时走 ambient 链路 + MeetingPipeline 链路
- 声纹分离：ECAPA-TDNN 分配说话人 ID（说话人1/说话人2），可用户改名并持久化
- 纪要生成：会议结束 → M2.7 总结 → `MeetingMinutes`（摘要 + 待办 + 决议）
- WS 实时推送：`meeting.state_change`、`transcript_segment`、`minutes_ready`

### 6.3 智能对话 Agent

多步工具循环，LLM 自主决策调用哪些工具：

| 工具 | 参数 | 功能 |
|---|---|---|
| `rag_search` | `query, top_k=20` | 本地知识库检索（ambient + 会议 + 上传文档） |
| `web_search` | `query, top_n=5` | Tavily + DDG 联网搜索 |
| `generate_artifact` | `artifact_type, brief, extra_instructions?` | 生成产物文件 |
| `final_answer` | `answer` | 输出最终 markdown 回答，结束本轮 |

**稳定性保障**：
- 单步 LLM 瞬时失败自动重试 1 次（避免云端抖动崩掉对话）
- 步数用尽（默认 6 步）不抛错，强制模型基于已有信息收尾
- 同工具+完全相同参数的重复调用被拦截（防止步数耗尽）
- 格式错误容忍 2 次重发，之后降级直接生成产物

### 6.4 一键产物生成（Skill）

支持 7 种产物类型：`pptx` / `word` / `xlsx` / `html` / `pdf` / `markdown` / `txt`

**Skill 路由逻辑**（`llm_skill.py`）：

```
pptx brief 含"投资/估值/目标价/DCF" → IB Deck（14 页投行风）
pptx 其他 → Strategy Deck（通用 IB 风，LLM 出 JSON + 固定模板）
word/xlsx brief 含财务词 → 财务模板
word/xlsx 其他 → 通用体裁自适应模板
html → K-Kami warm-parchment one-pager
```

**可靠性机制**：
- **JSON 修复**：`json-repair` 库兜底处理 LLM 漏逗号/多括号等非法 JSON
- **执行修复重试**：Python/Node 代码执行失败 → 把 stderr traceback 喂回 LLM 修复 → 重试 1 次
- **LLM 0-token 重试**：LLM 连通但 90s 内 0 token → 重试 1 次

### 6.5 RAG 知识库

**数据来源**：

- Ambient 转写（自动）
- 会议转写（自动，含说话人标签）
- 用户手动上传（PDF/Word/Excel/PPT/HTML/CSV/TXT）
- 工作区目录扫描（授权后自动增量索引）

**检索流程**（`retrieve_and_answer.py`）：

```
用户问题
  → Qwen3-1.7B 快速分类（是否需要检索 / 检索关键词扩展）
  → BM25Rag.query(top_k=1000)
  → diverse rerank（优先高分，同文档去重，Grep boost 关键词命中加权）
  → 截取 top-12 传主 LLM
  → 生成带引用角标的 markdown 回答
```

**文档解析**：markitdown 支持 PDF（含扫描件 OCR）、Word、Excel、PPT、HTML、CSV、Outlook 邮件

### 6.6 Hybrid 向量检索

- 默认关闭（`embedding_enabled=False`），启用后切换为 `HybridRag`
- Dense 向量：本地 BGE-M3（优先）→ Yunwu OpenAI embeddings（兜底）
- 向量存储：hnswlib cosine 索引 + sidecar JSON
- 融合：BM25 top-K + dense top-K → RRF 融合（k=60）→ 去重截断
- ingest 时 dense 向量异步写入，失败不阻塞 BM25 主链路

### 6.7 TTS 语音播报

- 后端：heyi-bj faster-qwen3-tts（音色 aiden）
- 输出：PCM 16kHz mono，前端 Web Audio API 播放
- 触发：Agent `final_answer` → 后端 `/tts/speak` → WS `tts.chunk` 推流 → 前端 `useTtsPlayer`
- 健康监控：`useTtsPlayer` 定期 `/tts/diag` 探针，StatusBar 显示状态
- 前端顶栏有 TTS 开关按钮

### 6.8 语音唤醒

STT 转写结果实时经 `extractEchoWakeCommand()` 检测，匹配则触发 Agent：

**支持的唤醒词变体**：
- 独立触发（无需前置寒暄）：`echo`, `echodesk`, `echoes`, `aiko`, `aico`, `pico`, `诶口`, `欸口`, `艾口`, `嘿口` 等
- 需要前置寒暄（`嘿/嗨/hey/hi/喂`）：`eco`, `ego`, `一口`, `一扣` 等
- 句首锚定 + 词边界校验，防止词中误触

### 6.9 意图路由

9 类意图（`/intent/route`），由 Qwen3-1.7B 快速分类：

| 意图 | 说明 |
|---|---|
| `chat` | 通用聊天/问答 |
| `rag_search` | 文档/知识库检索 |
| `web_search` | 联网搜索 |
| `generate_pptx/word/xlsx/html` | 生成指定类型产物 |
| `meeting_start/end/summary` | 会议控制 |

CommandBar 根据意图分发到不同后端链路。

### 6.10 说话人识别（声纹）

- ECAPA-TDNN 本地 CPU 推理（SpeechBrain）
- 会议内为每段 PCM 提取 d-vector，余弦相似度（阈值 0.55）分配说话人 ID
- 活动窗口 60s，最多 6 个 ambient 说话人
- 用户可对说话人改名，`label_user_set=True` 持久化到 `speakers` 表
- 会议外 ambient 模式也支持声纹，但默认 `diarizer_persist_speakers=False`

### 6.11 工作区文档扫描

- 用户在 SettingsPanel 授权目录路径
- 启动时自动增量扫描（`workspace_scan_on_startup=True`）
- 新增/修改文件 → markitdown 解析 → BM25/Hybrid 入库
- 单文件大小上限：100MB（`workspace_max_file_mb`）
- 支持手动触发 `POST /workspace/scan` 和清空 `POST /workspace/clear`

### 6.12 WebSocket 实时事件总线

**连接**：`WS /ws/echo`，`ClientHello` 握手，`last_seq` 重放机制防消息丢失

**Server → Client 事件（`EventType` 枚举）**：

| 事件 | 触发场景 |
|---|---|
| `meeting.state_change` | 会议开始/结束/纪要生成完成 |
| `transcript_segment` | 实时转写段（含说话人） |
| `minutes_ready` | 纪要生成完成（含 JSON） |
| `artifact.ready` | 产物生成完成（含 artifact_id） |
| `artifact.failed` | 产物生成失败（可重试） |
| `tts.chunk` | TTS PCM 音频帧 |
| `tts.suggest` | TTS 建议播放（不含音频） |
| `server_ping` | 30s 心跳，防止连接超时 |
| `server_resync` | 服务端要求前端重拉状态 |

---

## 7. 数据流详解

### 7.1 Ambient 采集流

```
麦克风 → 128ms PCM chunk
  → audio_gate.py（RMS 门控，过滤静音）
  → POST /capture/chunk
      ├─ wav.write（~/.echodesk/ambient/YYYY-MM-DD/）
      ├─ STT（FireRedASR2-AED）
      │   → Qwen3-1.7B 补标点（可选）
      ├─ 若转写非空：
      │   → ECAPA-TDNN 声纹（可选）
      │   └─ RAG.ingest(text, source="ambient-YYYYMMDD")
      └─ 若有活跃会议（meeting_id 非空 + meeting 未结束）：
          └─ MeetingPipeline.feed_segment(segment)
```

### 7.2 会议全链路

```
用户/AutoDetector → 开始会议 → POST /meetings/manual_start
                               → DB 建 meeting 记录
                               → WS push meeting.state_change(recording)

[音频帧持续流入 /capture/chunk]
  → MeetingPipeline
      → 累积转写段
      → DB 写 meeting_segments
      → WS push transcript_segment（实时显示）

用户 → 结束会议 → POST /meetings/manual_end
                → meeting 状态 = finalize
                → M2.7 生成纪要（分: 摘要/待办/决议）
                → DB 写 minutes_status = ready
                → WS push minutes_ready
                → RAG.ingest(纪要全文, source="meeting-{id}")
```

### 7.3 Agent 多工具链路

```
用户输入 "@echo 帮我调研竞品并生成分析报告"
  → CommandBar → intent 分类（chat/rag/agent → 走 Agent）
  → POST /agent/run（SSE）

  Step 0（预置）：
    含竞品/调研词 → 自动 prelude rag_search + web_search

  Loop（max 6 步）：
    → M2.7 输出 {"action":"tool_call","tool":"...","args":{...}}
    → 执行工具 → 结果喂回上下文
    → ...
    → M2.7 输出 {"action":"final","answer":"..."}
    → 流式 delta 推到前端 → TranscriptStream 显示

WS push artifact.ready → ArtifactPanel 新增卡片
```

### 7.4 产物生成链路

```
POST /artifacts/generate/stream（SSE）
  → SkillExecutorAdapter.generate_stream(kind, brief)

  [pptx] → _select_pptx_variant
    ├─ IB brief → _generate_ib_pptx_stream
    │   → 读 ib_master.pptx + llm_system_prompt.md
    │   → M2.7 生成 25 字段 JSON
    │   → node render.mjs（docxtemplater 渲染）
    │   → 生成 output.pptx（14 页 Goldman 风）
    └─ 通用 brief → _generate_strategy_pptx_stream
        → M2.7 生成扁平 slides+section JSON
        → json-repair 兜底修复
        → _STRATEGY_DECK_JS_TEMPLATE（pptxgenjs N-v3 IB 风）
        → node 子进程执行
        → 生成 output.pptx（封面+目录+章节扉页+内容+闭幕）

  [word] → _select_doc_variant
    ├─ 财务词 → WORD_SYSTEM（投研 Word）
    └─ 通用 → WORD_GENERAL_SYSTEM（体裁自适应 + 封面/目录模板）
    → M2.7 生成 python-docx 代码
    → python_executor.py 沙箱执行
    → [失败] → traceback 喂 LLM 修复 → 重执行（1次）

  → SkillProgress SSE（每步进度）→ 前端 StreamingArtifactCard
  → 完成 → WS push artifact.ready
  → GET /artifacts/{id}/download → 文件返回
```

---

## 8. PPT / Word / Excel / HTML 设计规范

### 8.1 PPT（IB 投行风 N-v3）

**来源**：2026-05-27/28 skill path 比较实验（JOURNAL.md），N-docxtemplater-v3 被用户定版。

**设计语言**（`_STRATEGY_DECK_JS_TEMPLATE`）：

| 元素 | 规格 |
|---|---|
| 背景（封面/闭幕/章节扉页） | 深海军蓝 `#001E3C` |
| 背景（内容页） | 米白 `#F5F2EA` |
| 强调色 | 暗金 `#C4953A` |
| 标题字体 | `Songti SC`（serif，中文优先）/ `Times New Roman` |
| 正文字体 | `PingFang SC` / `Helvetica Neue` |
| 封面 | 左侧暗金竖条 + serif 大标题 + 金色分割线 |
| 目录页 | 金色序号 + 章节名 + 虚线 leader |
| 章节扉页 | 整屏海军蓝 + 巨型金色序号（01/02/03…） |
| KPI 页 | hero 数字卡（白色卡 + 金色顶线 + 32pt serif 数字） |
| 表格 | 深蓝表头白字 + 米白/白斑马行 |
| 闭幕页 | 整屏海军蓝 + 居中金色 54pt serif 大字 |
| Chrome | 顶部金线 + 页码（金色）+ breadcrumb（章节名） |

**JSON Schema（LLM 输出）**：

```json
{
  "title": "封面主标题",
  "subtitle": "副标题",
  "footer": "来源：…",
  "closing": "感谢聆听",
  "closing_subtitle": "欢迎交流",
  "slides": [
    { "section": "章节名", "section_subtitle": "副标题", "title": "页标题", "bullets": ["要点"] },
    { "title": "指标页", "metrics": [{"value": "98%", "label": "标签"}] },
    { "title": "对比页", "table": {"headers": ["维度", "A", "B"], "rows": [["项目", "好", "差"]]} }
  ]
}
```

### 8.2 Word（标书/正式文档形式）

**设计参考**：`/Users/yoligehude/Downloads/技术要求响应文件.docx`（岚图汽车标书）

**正式长文档结构**（封面→目录→正文）：

| 元素 | 规格 |
|---|---|
| 页面 | A4（21×29.7cm），四边距 2.5cm |
| 封面 | 居中；公司名 18pt 黑体；项目大标题 26pt 粗体；文件类型 22pt 粗体；供应商/日期 14pt；密级红字 |
| 目录 | 标题「目  录」18pt 粗体；Word TOC 域（`TOC \o "1-3" \h \z \u`）自动生成页码 |
| 一级标题 | `add_heading(level=1)`，黑体海军蓝（`#1F3864`），含手写十进制编号（`1. 数据合规`） |
| 二级标题 | `add_heading(level=2)`，同色，`1.1 遵守法律法规` |
| 三级标题 | `add_heading(level=3)`，`1.1.1 总体响应` |
| 正文 | 11pt 宋体；首行缩进 |
| 颜色 | 海军蓝 `RGBColor(0x1F, 0x38, 0x64)` |

**体裁自适应**：prompt 引导 LLM 先判断体裁，短文档（通知/信函）不加封面目录，长文档（方案/标书/报告/制度）加完整骨架。

### 8.3 Excel（自适应表结构）

**体裁识别 → 结构设计**：

| 体裁 | 设计策略 |
|---|---|
| 清单/名册 | 单 sheet，冻结首行，列按属性 |
| 预算/费用 | 项目+金额+占比，SUM 公式，货币格式 |
| 统计/汇总 | 明细 + 汇总双 sheet |
| 对比/分析 | 行=维度，列=对象（或反转） |
| 排期/计划 | 日期+阶段+负责人+状态 |
| 财务模型（显式要求时） | 4 sheet（假设/财务/预测/DCF），DCF 公式，蓝/绿/黄色编码 |

**通用质量要求**：
- `ws.title`（非 `ws.name`）
- 冻结首行 `ws.freeze_panes = "A2"`
- 列宽自适应，数字/日期/百分比 `number_format`，表头底色+边框

### 8.4 HTML（Kami warm-parchment one-pager）

**来源**：K-Kami（tw93/Kami 5757⭐）定版，`JOURNAL.md` 用户评价"目前最好"。

**10 个不变量（invariants）**：

1. 暖羊皮纸背景 `#f5f4ed` + 墨蓝前景 `#1B365D`
2. Noto Serif SC 单一 serif 字体（无 bold/italic）
3. 无 box-shadow blur
4. 无 `rgba()`（WeasyPrint 兼容）
5. 纵向滚动 ≤ 2 屏（one-pager）
6. 不分页（非幻灯片）
7. 无 `#fff` 纯白
8. 无隐式 bold（`font-weight: 700`）
9. editorial 印刷感排版
10. 单文件 HTML（所有样式内联）

**体裁自适应**：LLM 先判断 brief 的题材（投资/产品/个人/报告等），再设计版块结构，不强套固定大纲。

---

## 9. 外部服务依赖

| 服务 | 用途 | 提供商 | 备注 |
|---|---|---|---|
| MiniMax-M2.7 | 主 LLM（会议纪要/产物生成/Agent） | Yunwu API | 按 token 计费 |
| Qwen3-1.7B | 快速 LLM（意图分类/补标点/修复重试） | heyi-bj | 本地 GPU 已部署 |
| FireRedASR2-AED | STT 语音转写 | heyi-bj（北京） | HTTP 流式 |
| faster-qwen3-tts | TTS 语音合成 | heyi-bj（北京） | PCM 16kHz |
| BGE-M3（可选） | 本地 dense embedding | 本地 CPU | Hybrid RAG 用 |
| text-embedding-3-large（兜底） | 云端 dense embedding | Yunwu | 仅 Hybrid RAG 启用时 |
| Tavily | Web 搜索主力 | Tavily API | 需 API Key |
| DuckDuckGo | Web 搜索兜底 | DDG 公共 API | 免费兜底 |
| SpeechBrain ECAPA-TDNN | 声纹分离 | 本地 CPU | Python 包 |

---

## 10. 测试覆盖

### 后端（`backend/tests/`）

| 类型 | 数量 | 关键覆盖 |
|---|---|---|
| unit | 500+ | agent_loop, hybrid_rag, vector_store, skill_doc_skills, meeting_pipeline, ambient_capture, diarizer, stt/tts adapter, ws_endpoint, admin_api, sqlite_repository, migrator, config_io, app_boot |
| integration | 10 | skill_e2e_yunwu, embedding_yunwu_real, stt_tts_heyi, real_audio_meeting, cross_meeting_memory, rag_and_web, llm_yunwu, meeting_e2e_yunwu, full_pipeline_e2e, intent_router_e2e |
| arch | 1 | `test_layer_dependencies.py`（CI Fitness Function 依赖规则校验） |
| **合计** | **554 passed** | 4 skipped（sqlite async lock 已知问题） |

### 前端（`desktop/tests/`）

| 类型 | 关键文件 |
|---|---|
| Mock E2E | agent-artifacts, artifact-generate, meeting-list, sad-paths, tts-flow, voice-wake, ws-reconnect, onboarding, rag-citations |
| 真服务 E2E | real-cloud-artifacts-and-voice, artifact-and-voice.real |
| 场景 | s01_first_run_and_about ~ s07_meeting_history（7 个完整用户场景） |
| **Mock E2E 全绿** | **41 passed** |

---

## 11. 关键设计决策（ADR 摘要）

| ADR | 决策 | 理由 |
|---|---|---|
| ADR-001 | **Yunwu 为主 LLM 通道**（非 self-host） | 5090 GPU 修复中；demo 期优先稳定性 |
| ADR-002 | **RAG only**，不上 mem0/LightRAG | RAG 实测 100% doc_cite 命中；图数据库复杂度不匹配当前规模 |
| ADR-003 | **Anthropic Skill 工作流**生成 4 类产物 | LLM 只出 JSON/Schema，固定模板渲染，比 LLM 直写代码稳定 |
| ADR-004 | **Ports-and-Adapters 架构** | use_cases 不 import 外部 SDK，可测试、可替换；CI 校验依赖规则 |
| ADR-005 | **单用户单机**，不上云存储 | demo 范围；数据不出机是核心承诺 |

**PPT Skill 选型实验结论**（2026-05-27/28，JOURNAL.md）：

- 实验了 15+ 条技术路径（Marp / Pandoc / html2pptx / pptxgenjs / docxtemplater / Touying / PPTAgent 等）
- 定版：**N-docxtemplater-v3**（Goldman Sachs sell-side 风格 14 页母版 + LLM 出 JSON + 固定渲染）
- 定版：**K-Kami warm-parchment**（tw73/Kami 10 invariants + LLM 一次性生成 HTML）
- 关键经验：LLM 出 JSON + 固定模板渲染 >> LLM 直写代码（稳定性、视觉质量双优）

---

*文档由 EchoDesk 开发记录自动整理，基于代码库实际状态（2026-06-01）。*
