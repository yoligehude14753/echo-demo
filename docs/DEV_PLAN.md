# Echo Demo · 开发计划（自上而下，按 PR 分批合并）

> **基准**：PRD v6.7.1 + ARCHITECTURE.md
> **节奏**：2 周 14 PR
> **commit 规范**：`feat(echodesk-<task-id>): 中文描述`
> **每个 PR ≤ 400 行 diff，self-review + AI review 后合并 main**

## PR 顺序（自上而下 + 自底向上交织）

### Sprint 0：脚手架（Day 1，**当前 batch**）

| PR | task-id | 内容 |
|---|---|---|
| **PR-1** | `bootstrap` | 项目骨架：FastAPI + Electron + .env + .gitignore + pre-commit + ruff/mypy + commitizen + GH Action skeleton |
| **PR-2** | `arch-fitness` | `tests/arch/` Fitness Function：校验 use_cases → adapters 单向依赖；加入 CI required check |
| **PR-3** | `ports-skeleton` | 定义 8 个 Port 抽象类 + Pydantic schemas（LLMPort/STTPort/...）+ unit test mock |

### Sprint 1：核心服务层（Day 2-3，自下而上铺 Adapter）

| PR | task-id | 内容 | 验收 |
|---|---|---|---|
| **PR-4** | `llm-adapter` | LLMAdapter：Yunwu M2.7（streaming 走通） + heyi-bj Qwen3-1.7B + retry/backoff + Pydantic 错误分类 | `pytest integration/test_llm_yunwu.py` 真 API 通 |
| **PR-5** | `stt-tts-adapter` | STTAdapter：FireRedASR2-AED WS 客户端；TTSAdapter | 真音频 → 真文本 |
| **PR-6** | `rag-adapter` | RAGAdapter：jieba+BM25 索引 + PDF 切片(pdfplumber) + SQLite 持久化 | 上传 NVIDIA 10-K → 查到 DC 段 |
| **PR-7** | `web-search-adapter` | WebSearchAdapter：Inspiro + Tavily + DDG + Qwen3-1.7B 仲裁器 | "英伟达最新 guidance" 出 Inspiro 主结果 |
| **PR-8** | `skill-adapter` | SkillAdapter：4 个 generator（pptxgenjs / python-docx / openpyxl-recalc / single-file-html），复用 v6.7.1 prompt | 4 产物 LibreOffice 全部可开 |

### Sprint 2：业务用例层（Day 4-7，按 user journey 闭环）

| PR | task-id | 内容 | 验收 |
|---|---|---|---|
| **PR-9** | `usecase-meeting` | meeting_summarizer 用例：STT 流 → 段落聚合 → M2.7 总结 → WS broadcast | 5min 录音 → 纪要正确分 sections |
| **PR-10** | `usecase-docqa` | doc_qa 用例：RAG + Web 仲裁 → M2.7 答 + 引用 | @"DC 营收占比" 答案含真引用 |
| **PR-11** | `usecase-artifact` | artifact_generator：纪要 → 4 种格式，含 fix loop（≤ 3 retries） | 纪要 → 30-90s 出 4 种产物 |
| **PR-12** | `usecase-intent` | intent_router：9 类 intent（summarize/query/search/calc/write/...） + 路由到对应 use_case | "@查最新财报" 走 doc_qa，"@算 DCF" 走 artifact |

### Sprint 3：前端 + WS（Day 8-10）

| PR | task-id | 内容 | 验收 |
|---|---|---|---|
| **PR-13** | `desktop-ws` | useEchoWS（含重连退避 + 异常断开单测）+ EchoRuntime + 协议边界单测 | 网络抖动/服务端断 → 自动恢复 |
| **PR-14** | `desktop-views` | ChatView 流式 / NotesPanel 清单 / Artifacts 画廊 / DocLibrary（Ant Design 5 + Tailwind） | 5 秒注视测试通过 |

### Sprint 4：E2E + Polish（Day 11-14）

| PR | task-id | 内容 | 验收 |
|---|---|---|---|
| **PR-15** | `e2e-meeting` | Playwright：真录音 12 分钟 NVIDIA 讨论 → 纪要 → 4 产物 | 完整 happy path 通过 |
| **PR-16** | `e2e-docqa` | Playwright：上传 PDF → @ 查询 → 引用正确 | sad path：空 RAG 命中 → 走 web |
| **PR-17** | `e2e-multispeaker` | Playwright：2 人对话录音 → 说话人 1/2 区分 → 纪要按 speaker | ECAPA-TDNN 默认参数下能区分 |
| **PR-18** | `demo-recording` | 完整 demo 录屏 + 兜底文案 + UI polish | 一次性跑 8 min demo |

## 业务目标三问（每个 PR 强制回答）

```markdown
1. 主路径可用？ [描述用户 happy path 是否完整跑通]
2. 失败路径有反馈？ [LLM 超时 / RAG 空命中 / Skill exec fail 都有 NotesCard 提示]
3. 状态集完整？ [会议状态/长任务状态/产物 ready 状态肉眼可见]
```

## E2E 测试矩阵（Sprint 4 强制）

| 场景 | Happy | Sad | 边界 |
|---|---|---|---|
| 会议纪要 | 12min 真录音 → 纪要 + 4 产物 | STT 中断恢复 / M2.7 超时 retry | 30 秒短录音 / 30 分钟长录音 |
| 文档 Q&A | 上传 NVIDIA 10-K → 查 5 个问题 | RAG 空命中 → web fallback / 编造 guard | 中英混合 / 大 PDF (50 页) |
| 一键产物 | 4 种格式各 1 次 | exec fail → fix loop → 失败 NotesCard | 极长纪要 / 极短纪要 |
| @指令 | 9 类 intent 各 1 次 | 模糊 intent → 用户确认 | 多 intent 串联 |
| 群聊声纹 | 2 人 5 分钟对话 | 单人 / 3 人 | 频繁打断 |

## 不做（demo 范围外）

- ❌ 多用户/账号系统
- ❌ 5090 host 24h 稳定性（host offline 期间用 Yunwu）
- ❌ 复杂权限/灰度/监控
- ❌ 飞书/企业微信集成
- ❌ memory_graph（用 RAG 替代）

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| Yunwu API 限速/降级 | 配置 fallback：M2.7 fail → GLM-4.6 fail → Kimi-2.6（按 token 计费走多通道） |
| Skill exec fix loop > 3 轮 | 降级到旧版 simple-prompt + python-pptx |
| 会议时长超 30 分钟 → STT 累计延迟 | 分 chunk 处理 + 滑动窗口总结，最多 5 分钟一段触发 M2.7 |
| 声纹默认参数不准 | demo 期接受错标，UI 文案"实验性功能"标记；不堵塞主流程 |
