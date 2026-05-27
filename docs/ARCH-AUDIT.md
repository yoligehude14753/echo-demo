# EchoDesk 架构事实基线（ARCH-AUDIT）

> 这是给 AI 助手 / 新人 / 自己未来的"真相单"——所有后续修复必须以本文为基线，
> 不能基于变量名 / 旧注释 / 摘要里的二手转述。
>
> 更新规则：发现任何架构事实不一致就立刻改这里，commit 一起进。

## 0. 项目身份

- 仓库：`echo-demo`（GitHub `yoligehude14753/echo-demo`），桌面 App 名 **EchoDesk**
- 老仓库 `echo`（`/Users/yoligehude/Desktop/all/echo`）是源头研究项目，
  echo-demo 是从它**简化 + Ports & Adapters 重构**而来，但很多决策来自 echo 实战
- 任何对架构的怀疑，先去 echo 源头核对，**不要看 echo-demo 的变量名做判断**

## 1. 模型 / 远程服务事实表（heyi-bj on Tailscale 100.87.251.9）

| 端口 | echo-demo `config.py` 命名 | swagger 实际服务 | 用途 | 状态 |
|---|---|---|---|---|
| `:7860` | `llm_fast_*` | `Qwen3-1.7B` (FastAPI) | 快速通道意图分类 + 短任务 | ✓ 活，名实相符 |
| `:8090` | `stt_firered_url` | `FireRedASR2-AED` | **主 STT**（默认 backend，arch-1 切回） | ✓ 活，名实相符 |
| `:8093` | `stt_sensevoice_gpu_url` | `SenseVoice GPU ASR` | STT fallback（`STT_BACKEND=sensevoice_gpu` 切回） | ✓ 活，名实相符 |
| `:8094` | `tts_qwen3_url`（alias `tts_cosyvoice_url`） | `faster-qwen3-tts CustomVoice` | 主 TTS | ✓ 活，命名已修正（arch-1） |

外部云：
- Yunwu / MiniMax-M2.7：会议纪要 / RAG / @生成 重路径 LLM
- 历史 fallback：GLM-4.6、Kimi-K2.6（未实战测过）

### ⚠️ 必须修复的命名误导

`backend/app/config.py` 行 60-62 把 Qwen TTS 服务全用 `cosyvoice` 命名：

```python
tts_provider: str = "cosyvoice"
tts_cosyvoice_url: str = "http://100.87.251.9:8094"   # 实际是 faster-qwen3-tts
tts_cosyvoice_voice: str = "aiden"                     # qwen3-tts 唯一 speaker
```

读 config 的任何人都会以为我们用 CosyVoice2-0.5B。**真实在跑的是 faster-qwen3-tts 1.7B CustomVoice**。
变量名 / provider 字符串 / TTS adapter 文件名都需要改 → 进入 PR `echodesk-arch-1` 修复范围。

## 2. STT 模型选型历史（echo 实战路径，非猜测）

按 commit 时间倒序：

```
9ca9d34  接入 heyi 自托管 FireRedASR2 + CosyVoice2     ← 接 FireRed
e84c8f8  添加 firered 熔断器，连续失败后快速回退        ← FireRed 不稳
5594c84  修复 FireRedASR2 幻觉三源问题                  ← 幻觉问题
c4930f8  切换 STT 后端到 faster-whisper large-v3        ← 弃 FireRed → Whisper
d20d62f  新增 STT LLM 后处理纠错（STT_LLM_CORRECT）
936381c  新增 SenseVoiceSmall STT 后端，并切换为默认    ← 终态：SenseVoice
7ec1914  接通声纹识别到主 STT 链路 + diarizer 强制 CPU   ← ECAPA 加入
```

**结论：SenseVoice 是 echo 实战胜出方案**。
echo `backend/app/config.py:114` 注释："sensevoice: 本地 in-process SenseVoiceSmall
（中英混合佳、带标点、**当前推荐**）"。

echo-demo 用 SenseVoice GPU（远程 :8093）= 推荐方案的远程部署版。

### STT 已知问题（基于 EchoDesk 本地 SQLite 数据 332 条 ambient）

- 73% segments < 30 字符
- 出现日文 / 英文碎片（"なんか" / "Yeah" / "Yes."）→ SenseVoice 没被强制中文
- echo-demo `config.py:55` 写了 `stt_language: str = "zh"`——**待验证 adapter 是否实际传给后端**

## 3. TTS 模型选型历史

```
9ca9d34  CosyVoice2-0.5B @ heyi-bj :8092
b065547  TTS 切到 faster-qwen3-tts 1.7B CustomVoice @ :8094  ← 终态
f465fe4  faster-qwen3-tts 24k→16k 内部重采样
```

echo 实战："TTFB 5ms，比 cosyvoice 200×"——所以换的。echo-demo 继承终态，但代码命名没跟上。

## 4. Diarizer / Speaker 链路（**炸点最严重**）

- 用 SpeechBrain ECAPA-TDNN 192-dim 余弦匹配（`backend/app/adapters/diarizer/ecapa.py`）
- 阈值 `diarizer_match_threshold = 0.65`
- `_MIN_DUR_FOR_NEW_PROFILE = 4.0` 死分支（chunk 永远 6s ≥ 4，guard 不触发）
- 接到 **ambient 持续采集链路**（`backend/app/use_cases/ambient_capture.py:94`）
  → 每 6s chunk 调一次

⚠️ echo 那边 ECAPA 接在**主 STT 链路**（不一定是 ambient）——echo-demo 把它接到 ambient
是引入 explosion 的核心变量。**待 archaeology 子任务 0def 报告确认。**

### 真实数据（EchoDesk SQLite，截至 2026-05-27）

```
speakers 表：81 条
  - 17 条 n_samples=1（孤儿幽灵）
  - 11 条 n_samples=2
  - 21 条 n_samples=3
  - 49/81 = 60% 几乎都是误判
ambient_segments 表：337 条
  - 同一段 1-2 分钟连续对话被切成 6 个不同 speaker
```

### 已识别的 root cause（按确定性排序，含 echo 考古校正 2026-05-27）

> 考古子任务确认：echo 也有约 **24.6% over-split**（audit 数据 57 label → 估 43 真人），
> 但 echo 的 ECAPA 做了 echo-demo 缺失的三件事，这是 explosion 量级差异的核心：

| # | 根因 | 确定性 | echo 怎么做的（archeology） | echo-demo 修法 | 阶段 |
|---|---|---|---|---|---|
| 1 | ECAPA `_profiles` 纯内存，重启丢光，`说话人 N` 跨重启贴错人 | 高 | echo **持久化 centroid 到 `speaker_profiles` 表**（`backend/app/db/schema.sql:192-200`），启动 hydrate | echo-demo 加 centroid 持久化（schema `embedding_blob` 已留位） | P0 |
| 2 | `_MIN_DUR_FOR_NEW_PROFILE = 4.0` 死分支（6s chunk 永远过） | 高 | echo 在 **VAD 句级 / 8-15s buffer flush** 调 diarizer，不是 6s 定长 → guard 真的有时触发 | echo-demo 改 VAD active 秒数门控 OR 切到 VAD 句级触发（架构级） | P0 |
| 3 | 阈值 0.65 对真实 6s 含噪音频过严 | 中→高 | echo `config.py:306` 默认 **0.70**（不是 0.65！）；env 可调；ECAPA fallback 0.65 | 提到 0.70 对齐，并加 env 覆盖 | P1 |
| 4 | diarizer 跑在 STT-hallu 门控之前，幻觉 chunk 已污染 `_profiles` | 高 | echo 在 `pipeline.py:652-678` 调 `identify_speaker`，**STT 成功后才跑**，不是并发 | echo-demo 改串行：STT 完 + hallu 过完 → 再 diarize | P1 |
| 5 | ring buffer 限 8 + 无 centroid 平均 | 高 | echo 做了 **EMA centroid 融合**（α=0.1 match / 0.03 fallback）+ ring history 8 条；centroid 落盘 | echo-demo 实现 EMA centroid（α 抄 echo） | P1 |
| 6 | 6s chunk 含静音/多人，单 embedding 失真 | 中 | echo `ServerVAD` 切到**句级**（end_silence_700ms，max_12s，min_0.5s），不是 6s 强切 | echo-demo 切 VAD 句级触发 diarizer（与 #2 合并修） | P2 |
| 7 | 音频预门控 `min_speech_frame_ratio=0.05` 过松（5%=0.3s 就过） | 中 | echo 这条门控由 ServerVAD 替代（700ms silence trim），无 5% 阈值 | echo-demo 加严或换 VAD 切片 | P1 |
| 8 | `diarizer_min_audio_bytes=16000`(0.5s) 死 gate | 高 | echo 内部 `duration < 1.0s 跳过`（diarizer.py:276），不靠 config | echo-demo 删 config 项，改硬编码 | P0 |
| 9 | 跨进程 `_counter` 漂移撞 ID | 高 | echo 的 ID 是从 DB hydrate 回来的，不靠运行时 counter | 同 #1 解决 | P0 |
| 10 | STT 语言乱跳（日英混入） | 已验证 language=zh 实际传了 | echo 也传 `language=zh`，但 echo 默认用 deepgram（中文路径不同） | 切 STT 模型解决（FireRed） | **本 PR** |
| 11 | **SenseVoice 在 6s 含静音/底噪 chunk 上 73% < 30 字 + 日英混入** | 高 | echo 实测 FireRed RTF=0.18，**中英混合 FRA2 保留英文**（不音译）；无 SenseVoice vs FireRed 数字 A/B | 切 STT 到 FireRed（heyi-bj :8090） | **本 PR** |

## 5. 链路图（当前真实，截至 2026-05-27）

```
[mic 6s chunk]
   ↓
ambient_capture.ingest_chunk
   ↓
pre_stt_gate (RMS+VAD)  ←─ ambient_rms_gate=600  ambient_min_speech_frame_ratio=0.05
   ↓ if gate.pass_:
   ├─ asyncio gather:
   │    ├─ _safe_stt → SenseVoice GPU :8093 ─→ TranscriptSegment[]
   │    └─ _safe_diarize → ECAPA local ─→ "speaker_N"
   ↓
is_likely_hallucination(text)  ←─ ambient_max_cps=12  ambient_min_stt_chars=4
   ↓ if hallu:
   │    texts = [], speaker_id = None  ⚠️ 但 ECAPA 内存已脏
   ↓
SpeakerRegistry.label_for(speaker_id) → "说话人 N"  ←─ 跨进程不可靠
   ↓
SQLite append_ambient_segment (text, speaker_label, ...)
   ↓
[optional] MeetingState.observe_chunk → 自动开会
[optional] MeetingPipeline overlay → 主会议链路
```

## 6. 待考古子任务（PARALLEL with PR-1）

- [ ] echo 那边 ECAPA 实际接哪条链路（subagent 0def）
- [ ] echo bench 数据里同人/异人余弦相似度分布（用真数据标 threshold）
- [ ] echo 是否做了 embedding 持久化（如有，借鉴实现）
- [ ] echo 是否强制了 STT language=zh

## 7. 修复路线图（不一次推完）

| PR | 范围 | 风险 | 阶段 | 状态 |
|---|---|---|---|---|
| `echodesk-arch-1` | STT 默认切回 FireRed；TTS 命名 cosyvoice→qwen3_tts；config 过期注释清理；ambient_capture 撒谎注释修正；ECAPA 死分支注释标 TODO；SenseVoice language=zh 真传到远程 | 极低 | P0 文档/命名 | ✅ 已完成 (commit `41b8216`) |
| `echodesk-ui-1` | TranscriptStream 改 Marvis 风格气泡（数字头像 + hover 时间） | 低 | P0 UI/UX | ✅ 已完成 (`46af67a`) |
| `echodesk-ui-2` | "人"计数与 TranscriptStream 显示同源（共享 `lib/speakerDisplay.ts`） | 低 | P0 UI/UX | ✅ 已完成 (`46af67a`) |
| `echodesk-spk-1` | **大包** 修 ARCH-AUDIT §4 root **#1 #3 #4 #5 #8 #9**：embedding 持久化+hydrate；EMA centroid（α=0.1）；阈值 0.65→0.70；diarize 串行到 STT-hallu 之后；删 `diarizer_min_audio_bytes` config，硬编码 1.0s | 中 | P0 | ✅ 已完成 (`c468fc1`) |
| `echodesk-spk-2` | diarizer 触发改为 VAD 句级（而非 6s 固定 chunk）→ 修 #5b。`audio_gate.split_into_voiced_segments` + `ECAPADiarizer.identify_segments` | 中 | P1 | ✅ 已合 (`a00d54b`) |
| `echodesk-spk-3` | 删 `_MIN_DUR_FOR_NEW_PROFILE`/`_OUTLIER_SIM_ALLOW_NEW` 硬编码 → 改成 settings 可配的 voiced active seconds 门控；短段不允许注册新人，sim 不足直接丢弃（不污染已知 centroid） | 低 | P1 | ✅ 本 PR |
| `echodesk-spk-4` | ambient pre-gate / hallucination 阈值收紧 → 修 #6 #7 | 低 | P1 | ✅ 已合 (`6aa3d00`) |
| `echodesk-spk-5` | 清库 + 真实多人音频回归 + 阈值闭环标定 | 中 | P1 | ⏳ 待 spk-3 合后 |

### 新增模块（用户 2026-05-27 反馈）

- 用户截图："175 段 · 86 人"，TranscriptStream 同时显示"说话人 47"
- 根因：`store.ts` 行 89-90 把后端发来的 raw global label 累加进 `Set`，
  TranscriptStream 用最近 100 条 ambient 做 remap → **两个数字源不同步**
- 短期修：ui-2 让人数 = `Math.max(...remappedIdx)`，与气泡上看到的最大编号一致
- 中期修：spk-1..5 把后端 distinct speaker 数压回真实值（~3-5 人）后，86 自然落回

---

**最后更新**：2026-05-27 by AI assistant after `echodesk-spk-3` lands  
**下次更新触发**：spk-5 合入 / 真实多人回归出数据
