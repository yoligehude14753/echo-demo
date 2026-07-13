# EchoDesk 0.3.1 降级与恢复行为

> 远程依赖断开时，**核心录音/转写链路必须不挂**；只让依赖远程的具体功能降级。

## 远程依赖映射

| 远程 | 端口 | 依赖它的功能 | 不依赖它的功能 |
|---|---|---|---|
| eight FireRedASR2-AED | `:8090` | 转写（ambient + meeting） | 录音 / TTS / @生成 / RAG |
| eight qwen3-tts | `:8094` | TTS 播报 | 录音 / 转写 / @生成 / RAG |
| Yunwu MiniMax-M2.7 fast fallback | api | intent 分类、RAG/web 仲裁 | 录音 / 转写 / TTS |
| Yunwu deepseek-v4-flash | api | @生成、纪要、@查 RAG 答 | 录音 / 转写 / FAST 分类 |
| Tavily 搜索 | api | @查 web fallback | 全部其它 |

## 各 use_case 的失败处理

### Artifact workflow（@生成 HTML/PPT/Word/Excel）

- **LLM / Skill / sanitize / timeout 失败** → Workflow 与 Artifact 同步进入可观察失败态，保留错误类别和原 run。
- **前端**：outputs 展示失败卡片与真实重试；重试创建新 run 并保留 lineage，不修改旧终态。
- **一致性**：domain write、run/event 与 outbox 同一 Unit of Work，失败不会留下“Artifact 成功、Workflow 缺失”的半提交。

### chat（流式问答）

- **LLM 失败**（任何阶段）→ SSE 以 error frame 终止；renderer transport 将其转换为失败态，不显示“已回复”。
- **前端**：输入恢复可用并展示经过清洗的错误提示；不会吞错，也不会自动重放非幂等请求。

### retrieve_and_answer（RAG + web 仲裁）

- **fast LLM 分类失败** → fallback 到 `"either"`（两条检索都跑，让 main LLM 综合）
- **RAG 检索失败** → swallow 成空结果
- **web 搜索失败** → swallow 成空结果
- **main LLM 流失败** → 进入 Chat SSE 的显式失败边界。

### intent_router（用户输入分类）

- **LLM 失败** → fallback 到 `kind=chat`、`confidence=0.3`、`rationale=LLM 失败兜底`
- **代码**：`backend/app/adapters/intent/llm_router.py:99-101`

### meeting_pipeline.finalize_meeting（生成纪要）

- **LLM 失败** → finalize workflow 进入 failed，meeting 不伪装为已生成纪要。
- **meeting 状态**：`minutes_status` / `minutes_error` 可持久恢复；详情瞬时请求失败不会永久标记为已加载。
- **前端**：状态栏和纪要区显示错误并允许用户明确重试。

### TTS

- **远程 TTS 失败** → 停止当前播放并展示错误消息，不影响录音、转写和其它 workflow。
- **前端**：StatusBar 的 TTS 子探针同步反映失败；不会出现“按钮显示已播报但实际静音”。

## UI surface（P2.1 StatusBar）

用户**不需要**翻 log 就能看到降级：

- `backend` pill 绿 = supervisor + /healthz/full 通
- `eight` pill 绿 = STT/TTS/Fast-LLM 三子探针都通；当前 public Fast-LLM 默认走 Yunwu 兜底，eight 机器只要求 STT/TTS 通
- `云` pill 绿 = Yunwu + Tavily 都通；橙 = 缺 API key（功能不可用但不是"挂了"）；红 = key 配了但断
- `麦克风` pill 绿 = 系统权限 granted

降级时用户工作流：
1. 看 status pill 判断哪条远程断
2. 点 pill 弹 Popover 看 latency / error 详情
3. 等待恢复 或 改 `~/.echodesk/config.json` 换 endpoint

## 不做的事

- 不自动重放非幂等业务请求；Artifact / Meeting / Agent 的重试由用户明确触发并形成新 run。
- HTTP session 续签、WS 断线与 outbox 投影使用有界恢复；身份失效、最低版本不满足和 terminal conflict 会停止重试。
- 不做未声明的自动多级 provider 切换；MAIN / FAST fallback 只按 ADR-001 的显式配置执行。
- 不做离线模式：录音/转写依赖 STT，没 STT 就只能录但不能转写
