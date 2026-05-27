# Echo Demo · 架构（自上而下）

> 严格遵循 alld 核心原则：**复用优先于自研** / **业务核心只依赖 Port 接口** / **外部 I/O 必须可重试可观测** / **业务目标先于接口验收**

## 0. 顶层视图

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Desktop (Electron + React)                  │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  ChatView   │  │ NotesPanel  │  │ Artifacts   │  │ DocLibrary  │  │
│  │ (会议流)    │  │ (清单)      │  │ (产物画廊)  │  │ (上传/RAG)  │  │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  │
│         │                │                │                │         │
│         └────────────────┴────────────────┴────────────────┘         │
│                              │                                       │
│                       useEchoWS / EchoRuntime                        │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ WebSocket (msgpack) + REST
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Backend (FastAPI · port 7777)                  │
│                                                                      │
│  Layer 4: API (FastAPI routes + WS)                                  │
│  ├── /api/meeting/{start,stop,upload,query}                          │
│  ├── /api/doc/{upload,list,query}                                    │
│  ├── /api/artifact/{generate,download}                               │
│  ├── /api/intent/route   (9 类 @ 指令路由)                           │
│  └── /ws/echo            (流式 meeting state + 纪要 chunks)          │
│                                                                      │
│  Layer 3: Use cases (业务编排，纯函数)                                │
│  ├── meeting_summarizer.py     STT → diarize → M2.7 → broadcast      │
│  ├── doc_qa.py                  RAG + Web 仲裁 → M2.7 答 + 引用        │
│  ├── artifact_generator.py      MeetingNotes → Skill → PPT/Word/...  │
│  └── intent_router.py           prompt classifier → tool call         │
│                                                                      │
│  Layer 2: Ports (Adapter 接口 — 业务代码只 import Port)               │
│  ├── LLMPort       (call_llm / call_llm_with_schema)                 │
│  ├── STTPort       (transcribe_stream)                               │
│  ├── TTSPort       (synthesize)                                      │
│  ├── DiarizerPort  (identify_speakers)                               │
│  ├── RAGPort       (index_doc / query_doc)                           │
│  ├── WebSearchPort (search → arbitrate)                              │
│  ├── SkillPort     (generate_artifact[ppt|word|xlsx|html])           │
│  └── Repository    (save_meeting / load_doc / ...)                   │
│                                                                      │
│  Layer 1: Adapters (外部依赖具体实现)                                  │
│  ├── adapters/llm/yunwu_m27.py             (Yunwu M2.7 主通道)        │
│  ├── adapters/llm/heyi_qwen17.py           (heyi-bj Qwen3-1.7B fast)  │
│  ├── adapters/stt/heyi_firered.py          (heyi-bj FireRedASR2-AED)  │
│  ├── adapters/tts/heyi_tts.py              (heyi-bj)                  │
│  ├── adapters/diarize/local_ecapa.py       (SpeechBrain 默认)         │
│  ├── adapters/rag/jieba_bm25.py            (jieba+rank_bm25)          │
│  ├── adapters/web/inspiro.py + tavily.py + ddg.py                    │
│  ├── adapters/skill/pptxgenjs.py           (Node 子进程)              │
│  ├── adapters/skill/python_docx.py                                   │
│  ├── adapters/skill/openpyxl_recalc.py                               │
│  ├── adapters/skill/html_dashboard.py                                │
│  └── adapters/repo/sqlite.py                                         │
│                                                                      │
│  Layer 0: Infra                                                      │
│  ├── config (pydantic-settings, .env)                                │
│  ├── observability (structlog + Prometheus)                          │
│  └── db (SQLite + alembic)                                           │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          External Services                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │   Yunwu     │  │   heyi-bj   │  │   Inspiro   │  │   Tavily    │  │
│  │  M2.7 API   │  │  STT/TTS    │  │ Web Search  │  │  fallback   │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

## 1. 模块依赖规则（CI Fitness Function 校验）

- `app/api` 只能 import `app/use_cases` 和 `app/schemas`
- `app/use_cases` 只能 import `app/ports` 和 `app/schemas`，**禁止** import `app/adapters`
- `app/adapters/*` 只能 import 对应的 `app/ports/*` 和外部 SDK
- 业务代码（use_cases）**禁止**裸 `import openai / requests / sqlalchemy.Base`，必须用 `yoli_llm / yoli_http / yoli_db`（中台门面）

## 2. 数据流（核心 user journey）

### 2.1 音频采集 vs 会议（两个正交域 · 方案 2）

| 域 | 组件 | 生命周期 | 用户控制 | 数据去向 |
|---|---|---|---|---|
| **CaptureSession** | `capture/AudioCapture` + `POST /capture/chunk` | App 启动 → 退出 | **无**（24h 持续） | 落盘 `ambient/` + STT + RAG（`source=ambient`） |
| **MeetingSession** | CommandBar `@开始/结束/总结会议` | idle → in_meeting → ended | **手动** | 叠加：转写流 + diarization + 纪要 |

路由规则（`capture/captureChunkRouter.ts` 唯一写入点）：

```
每个 PCM chunk
  └─ POST /capture/chunk（主链路，永远执行）
        ├─ 落盘 ~/.echo-demo/ambient/YYYY-MM-DD/*.wav
        ├─ STT → 非空则 RAG ingest（ambient-YYYYMMDD）
        └─ 若 meeting_id 且 meeting 未 ended → MeetingPipeline 叠加（同一次 STT 结果）
```

会议外音频**不丢弃**，是个人数字分身的 ambient 记忆层。

### 2.2 文档 Q&A + Web 仲裁

```
[上传 PDF] → backend /api/doc/upload
          → pdfplumber 切片 → jieba 分词 → BM25 索引
          → SQLite 持久化
[用户 @echo "DC 营收同比"] → desktop ChatView 触发
                          → backend /api/intent/route
                          → 分类: doc_query
                          → RAGPort.query (top 5) + WebSearchPort.search (Inspiro)
                          → 仲裁器 (Qwen3-1.7B 信心打分)
                          → M2.7 合成 + 引用 footnote
                          → NotesCard 渲染答案 + 来源
```

### 2.3 一键产物（PPT/Word/Excel/HTML）

```
[纪要存在] + [用户 @ 指令"生成 PPT 给老板看"]
           → intent_router → artifact.gen
           → SkillPort.gen_ppt(notes, theme=midnight_executive)
             → call Yunwu M2.7 生成 pptxgenjs JS code
             → Node 子进程执行
             → 拿到 .pptx
             → LibreOffice headless 验证可打开
           → 失败 → fix loop (≤ 3 retries)
           → ChatView NotesCard "📊 nvidia_outlook.pptx" + [下载]
```

### 2.4 群聊式发言人

```
DiarizerPort.identify(audio_chunk) → speaker_id (本会议局部 ID: 1/2/3)
                                    → ChatView 渲染时左侧 avatar 色码
                                    → 纪要里以 "说话人 1:" / "说话人 2:" 区分
```

## 3. WebSocket 协议

```jsonc
// server → client
{
  "type": "meeting.state",
  "state": "recording" | "summarizing" | "done" | "error",
  "elapsed_s": 765
}
{
  "type": "transcript.chunk",
  "speaker": "1" | "2" | null,
  "text": "我们 Q3 的 DC 营收同比 ...",
  "ts": 1717000000.0
}
{
  "type": "notes.update",
  "section": "decisions" | "action_items" | "summary",
  "content_md": "..."
}
{
  "type": "artifact.ready",
  "kind": "ppt" | "word" | "xlsx" | "html",
  "url": "/api/artifact/abc123.pptx",
  "preview_thumbnail": "data:image/png;base64,..."
}
{
  "type": "intent.processing",
  "intent_id": "abc",
  "label": "正在搜索英伟达最新财报…"
}

// client → server
{
  "type": "audio.chunk",
  "data": "<binary>",
  "session_id": "..."
}
{
  "type": "user.message",
  "text": "@echo 总结一下刚才的讨论",
  "session_id": "..."
}
```

## 4. 部署拓扑

| 组件 | 位置 | 资源 |
|---|---|---|
| desktop (Electron) | Mac 本地 | 用户机器 |
| backend (FastAPI) | Mac 本地 → 上线后 GCP VM | dev: 本地 / prod: e2-medium |
| Yunwu API | cloud | 按 token 计费 |
| heyi-bj (STT/TTS/Qwen 1.7B) | 北京 GPU | 已部署稳定 |
| heyi-91 (M2.7 self-host) | 上海 5090 | **demo 期 SKIP**，host 修好再启用 |
| SQLite | 本地文件 `~/.echo-demo/data.db` | 单文件 |
| 上传文件 | 本地 `~/.echo-demo/storage/` | 单机 |

## 5. 不可逆决策（已写 ADR）

详见 `docs/adr/`：
- `ADR-001-yunwu-as-primary-llm-channel.md` — Yunwu 为 demo 期主通道（5090 修好后切回 self-host）
- `ADR-002-rag-only-no-memory-graph.md` — 跨会议记忆只走 RAG，不上 mem0/LightRAG
- `ADR-003-anthropic-skill-workflow.md` — 4 产物用 Skill workflow
- `ADR-004-ports-and-adapters.md` — 业务核心只依赖 Port，外部依赖 Adapter
- `ADR-005-no-multi-user-no-cloud-storage.md` — demo 范围单用户单机

## 6. 测试金字塔

- **unit**: 各 use_case 用 mock Port，覆盖 happy path + sad path（≥ 80%）
- **integration**: 真 Adapter 接真服务（Yunwu / heyi-bj / Inspiro），跑 P0 三场景
- **E2E**: Playwright 驱动 Electron + 真录音/真 PDF，跑完整 demo 脚本
- **fitness**: CI 阶段跑 `tests/arch/` 校验依赖规则（use_cases 不许 import adapters）
