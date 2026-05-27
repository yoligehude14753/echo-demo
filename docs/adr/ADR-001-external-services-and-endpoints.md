# ADR-001 · 外部服务与 endpoint 一次性固化

> 日期：2026-05-26 17:35
> 状态：Accepted
> 触发：用户反馈"重要信息丢了"——之前 PRD 里 STT/TTS 端点信息没有写全，需要查 echo 仓库的 .env.heyi.example + config.py + stt.py 才能确认实际部署形态。本 ADR 一次性固化所有外部依赖，新项目所有 adapter 都引用本表。

## 决策

### 1. LLM

| 角色 | provider | model | base URL | API key 变量 |
|---|---|---|---|---|
| **MAIN（主，复杂任务）** | **Yunwu** | `MiniMax-M2.7` | `https://yunwu.ai/v1` | `YUNWU_OPEN_KEY=sk-OtwU0M3oBDEsicKSqxizBG8ma1fVqv3oUEOe3yV7wbtbydui` |
| 备用 1（限速时切） | Yunwu | `GLM-4.6` | 同上 | 同 key |
| 备用 2（限速时切） | Yunwu | `Kimi-K2.6` | 同上 | 同 key |
| **FAST（路由/extract/快速回复）** | heyi-bj local vLLM | `Qwen3-1.7B` | `http://100.87.251.9:7860/v1` | `LLM_LOCAL_API_KEY=EMPTY` |
| ~~self-host (roadmap)~~ | ~~heyi-91 self-host~~ | ~~MiniMax-M2.5~~ | ~~`http://100.73.254.81:10814/v1`~~ | host **OFFLINE**，demo 期不依赖 |

### 2. STT

| 后端 ID | 部署位置 | URL | 模型 | 状态 |
|---|---|---|---|---|
| **`sensevoice_gpu`** (主) | heyi-bj GPU 容器 :8093 | `http://100.87.251.9:8093/v1` | `sensevoice-small` | 默认主通道 |
| `sensevoice` (备) | 本地 in-process | — (FunASR AutoModel) | `iic/SenseVoiceSmall` (~500MB CPU) | Mac dev 时用；GPU API 熔断时 fallback |
| `firered` (备) | heyi-bj :8090 | `http://100.87.251.9:8090/v1` | FireRedASR2-LLM-8B | 已实测有幻觉，不推荐 |
| ~~deepgram~~ | cloud | — | Nova-3 | demo 期 disabled（全本地化对齐） |

**输入约定**：原始 PCM 16kHz 16-bit mono，bytes。

### 3. TTS

| 后端 ID | 部署位置 | URL | 实际服务 swagger title | 状态 |
|---|---|---|---|---|
| **`qwen3_tts`** (主) | heyi-bj :8094 | `http://100.87.251.9:8094` | `faster-qwen3-tts CustomVoice OpenAI-compatible API` | TTFA < 500ms |
| `openai` (备) | Yunwu | `https://yunwu.ai/v1/audio/speech` | — | 按 token 计费 |

**命名校正（2026-05-27）**：原 ADR 把这个 backend ID 写作 `cosyvoice`，因为 :8094
最早是 cosyvoice2-0.5B（echo commit 9ca9d34），后被 echo commit b065547 替换为
faster-qwen3-tts 1.7B（TTFB 5ms，比 cosyvoice 200×）。echo-demo 继承终态但
变量名忘了改。本 ADR + `config.py` 都已更名为 `qwen3_tts`；旧 env 变量
`TTS_COSYVOICE_*` 通过 pydantic AliasChoices 保留向后兼容。详见 docs/ARCH-AUDIT.md §3。

### 4. Speaker Diarization（声纹识别）

| 决策 | 值 |
|---|---|
| backend | `ecapa` (SpeechBrain ECAPA-TDNN 192-dim) |
| 部署 | 本地 in-process（首次下载 85MB 到 `~/.cache/speechbrain/`）|
| 阈值 | `0.65`（spike 2026-05-25 实测 6 人混合会议 DER=0.0%，sweep 0.65-0.75 全过） |
| 最短片段 | 16000 bytes ≈ 0.5s |
| 用户决策 (2026-05-26 17:23) | "声纹按现状就行" → 不 fine-tune，用默认参数 |

### 5. Web Search 仲裁

> 2026-05-26 17:38 用户决策：**"就用tavily，别用inspiro了"** → 跳过 Inspiro 主通道，简化为 Tavily + DDG 双层。

| 优先级 | provider | URL | API key |
|---|---|---|---|
| **1 (主)** | Tavily | (SDK 内置) | `TAVILY_API_KEY=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` |
| 2 (兜底) | DDG | — | 无需 key |

仲裁器：用 Qwen3-1.7B (FAST 通道) 对 RAG vs Web 两个结果做置信度打分（pre_classifier + fabrication_guard，复用 echo backend `app/web_arbitration.py` 思路）。

Inspiro 留作未来候选（如果 Tavily 质量退化或额度超限时切回）。

### 6. Skill 执行器（4 产物，复用 v6.7.1）

| 产物 | toolchain | 系统依赖 |
|---|---|---|
| PPT (.pptx) | `pptxgenjs` (Node.js) | `npm install -g pptxgenjs` + Node.js 24+ |
| Word (.docx) | `python-docx` | `pip install python-docx` |
| Excel (.xlsx) | `openpyxl` + LibreOffice recalc | `pip install openpyxl` + LibreOffice 26+ headless |
| HTML (.html) | single-file + Tailwind CDN | 无系统依赖 |

**fix loop 上限**：3 轮。超出降级到 PRD §A.2.10 旧版（simple-prompt + python-pptx）。

## 网络拓扑（关键 IP）

| 主机 | tailnet IP | 角色 |
|---|---|---|
| **heyi-bj** | `100.87.251.9` | STT/TTS GPU 容器 + Qwen3-1.7B vLLM + ECAPA cache |
| ~~heyi-sh-5090~~ | ~~`100.73.254.81`~~ | M2.7 self-host **OFFLINE** |
| 本机 Mac | localhost | desktop + backend dev |
| Yunwu | `https://yunwu.ai` | cloud LLM 主通道 |

**Tailscale 必须开**：从 Mac 本地访问 `100.87.251.9` 的 STT/TTS/Qwen 都要走 tailnet。dev 时确保 `tailscale status` 显示 heyi-bj online。

## 影响

- backend `app/adapters/llm/yunwu_m27.py` 直接读 `YUNWU_OPEN_KEY`
- backend `app/adapters/stt/heyi_sensevoice_gpu.py` 默认走 :8093
- backend `app/adapters/tts/qwen3_tts.py` 默认走 :8094（旧名 `cosyvoice.py`，2026-05-27 重命名）
- backend `app/adapters/diarize/local_ecapa.py` 默认 `DIARIZER_BACKEND=ecapa` + 阈值 0.65
- backend `app/adapters/web/inspiro.py` + `tavily.py` + `ddg.py` 仲裁

## 失败回退路径

| 服务 | 主 down 时降级路径 |
|---|---|
| Yunwu M2.7 | → Yunwu GLM-4.6 → Yunwu Kimi-K2.6 → UI 报错"主通道不可用，请重试" |
| heyi-bj :8093 STT | 熔断 3 次 60s → 本地 in-process `sensevoice` |
| heyi-bj :8094 TTS | → openai TTS via yunwu |
| heyi-bj :7860 Qwen | → 直接走 MAIN（M2.7）做 fast tasks（成本高但兜底） |
| Inspiro | → Tavily → DDG → 仲裁器把"无 web 结果"传给 LLM |
| Skill exec | 3 次 fix 失败 → 简单 prompt + python-pptx 兜底 |

## 后续

- 5090 host 恢复后追加 ADR-009：M2.7 切回 self-host
- Inspiro 新 key 可用后追加 ADR-010：Web search 主通道复跑 P0-2 v4 对照
