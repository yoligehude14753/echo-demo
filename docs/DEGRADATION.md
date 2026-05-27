# EchoDesk 降级行为（P2.3）

> 远程依赖断开时，**核心录音/转写链路必须不挂**；只让依赖远程的具体功能降级。

## 远程依赖映射

| 远程 | 端口 | 依赖它的功能 | 不依赖它的功能 |
|---|---|---|---|
| heyi-bj FireRedASR2-AED | `:8090` | 转写（ambient + meeting） | 录音 / TTS / @生成 / RAG |
| heyi-bj qwen3-tts | `:8094` | TTS 播报 | 录音 / 转写 / @生成 / RAG |
| heyi-bj Qwen3-1.7B | `:7860` | intent 分类、RAG/web 仲裁 | 录音 / 转写 / TTS |
| Yunwu MiniMax-M2.7 | api | @生成、纪要、@查 RAG 答 | 录音 / 转写 / 分类 |
| Tavily 搜索 | api | @查 web fallback | 全部其它 |

## 各 use_case 的失败处理

### artifacts.generate（@生成 HTML/PPT/Word/Excel）

- **LLM 失败**（Yunwu 断 / timeout）→ emit `artifact.failed` event 含 `reason=remote_llm`，HTTP 502
- **Skill 执行失败**（生成代码错 / sanitize 失败）→ emit `artifact.failed` event，HTTP 400
- **前端**：P2.2 的 `<FailedArtifactCard>` 渲染失败卡片 + 重试按钮
- **代码**：`backend/app/api/artifacts.py:67-90`

### chat（流式问答）

- **LLM 失败**（任何阶段）→ SSE 流里 push `{"error": "..."}` + `[DONE]`，HTTP 仍 200
- **前端**：store 收到 error 字段后应渲染降级提示（**TODO**：当前前端可能没渲染这个字段；下个 frontend PR 加）
- **代码**：`backend/app/api/chat.py:50`

### retrieve_and_answer（RAG + web 仲裁）

- **fast LLM 分类失败** → fallback 到 `"either"`（两条检索都跑，让 main LLM 综合）
- **RAG 检索失败** → swallow 成空结果
- **web 搜索失败** → swallow 成空结果
- **main LLM 流失败** → propagate 到 chat.py 的 SSE catch
- **代码**：`backend/app/use_cases/retrieve_and_answer.py:84-103`

### intent_router（用户输入分类）

- **LLM 失败** → fallback 到 `kind=chat`、`confidence=0.3`、`rationale=LLM 失败兜底`
- **代码**：`backend/app/adapters/intent/llm_router.py:99-101`

### meeting_pipeline.finalize_meeting（生成纪要）

- **LLM 失败** → raise `MeetingPipelineError` → HTTP 500
- **meeting 状态**：不变成 `finalized`，用户能重试
- **前端**：toast 错误 + 状态栏 Yunwu pill 应已是红色
- **TODO**：可以 emit 新 `minutes.failed` event 让前端展示失败卡片（Phase 3）

### TTS

- **远程 TTS 失败** → 静默不播报（不影响其它）
- **前端**：StatusBar 的 heyi pill 会反映 TTS 子探针失败

## UI surface（P2.1 StatusBar）

用户**不需要**翻 log 就能看到降级：

- `backend` pill 绿 = supervisor + /healthz/full 通
- `heyi-bj` pill 绿 = STT/TTS/Fast-LLM 三子探针都通；橙 = 部分通；红 = 全断
- `云` pill 绿 = Yunwu + Tavily 都通；橙 = 缺 API key（功能不可用但不是"挂了"）；红 = key 配了但断
- `麦克风` pill 绿 = 系统权限 granted

降级时用户工作流：
1. 看 status pill 判断哪条远程断
2. 点 pill 弹 Popover 看 latency / error 详情
3. 等待恢复 或 改 `~/.echodesk/config.json` 换 endpoint

## 不做的事

- 不做自动重试（除 STT FireRed 自身的 circuit breaker）：让 LLM 调用幂等性由用户判断
- 不做远程切换 fallback（M2.7 → GLM-4.6 → Kimi）：当前只演示 Yunwu，配置一份就够
- 不做离线模式：录音/转写依赖 STT，没 STT 就只能录但不能转写
