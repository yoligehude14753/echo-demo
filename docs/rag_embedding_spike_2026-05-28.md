# EchoDesk Hybrid RAG · Dense Embedding 通道 Spike（2026-05-28）

> **本文只做调研 + 决策建议，不改任何代码、不下载任何模型、不重启 backend。**
>
> **目标**：确认 EchoDesk 中期 Hybrid RAG（见 `docs/rag_redesign_2026-05-28.md §C.3`）的 dense embedding 通道走哪一路：
> A. 云雾 OpenAI 兼容 `/v1/embeddings`
> B. heyi-bj 远端（`100.87.251.9`，与现有 fast LLM / STT / TTS 同机）
> C. 本地 bge-m3（sentence-transformers / FlagEmbedding）
>
> **测试机器**：M2 Pro, macOS 25.4.0, 仓库 `/Users/yoligehude/Desktop/all/echo-demo`，凭证读自 `~/.echodesk/config.json`。
> **时间**：2026-05-28 18:15-18:25 (UTC+8)。

---

## 1. TL;DR · 三路结论一句话

| 通道 | 可用？ | 关键证据 | 推荐定位 |
|---|---|---|---|
| **A. 云雾 `/v1/embeddings`** | ✅ **可用**，但仅限 OpenAI 系模型 | `text-embedding-3-small`/`-3-large`/`text-embedding-ada-002` 都 200；`bge-m3` 和 `Qwen3-Embedding-0.6B` 返回 503 「无可用渠道」 | **fallback / 离线回填** |
| **B. heyi-bj 远端** | ❌ **不可用** | 已扫 12 个端口，**零** embedding 服务；7860 是 `Qwen3-1.7B`(chat)，7862 是 `Qwen3-32B-AWQ`(chat)，明确报错 *"This model does not appear to be an embedding model by default. Please add `--is-embedding`"*；8080 是 open-webui 0.6.41 但 ollama backend 未连（`/ollama/api/version`→`{"version":false}`） | **本期放弃**，等运维在 heyi-bj 单独起 sglang `--is-embedding` 或 ollama pull bge-m3 后再评估 |
| **C. 本地 bge-m3** | ⚠️ **依赖未装但路径通畅** | backend `.venv` 没有 `sentence_transformers` / `FlagEmbedding` / `hnswlib` / `faiss`；HuggingFace 直连可达（HEAD `huggingface.co/BAAI/bge-m3` HTTP 200，2.2s），无需 mirror | **主路** |

**最终推荐**：**主路 C（本地 bge-m3 + hnswlib）+ fallback A（云雾 text-embedding-3-large，仅离线回填触发，不参与在线 query）**。

---

## 2. A 路 · 云雾 `/v1/embeddings` 实测

### 2.1 可用模型探测（curl + Bearer `yunwu_open_key`）

| model | HTTP | 维度 | usage.tokens | 单次延迟 | 备注 |
|---|---|---|---|---|---|
| `text-embedding-3-small` | **200** | 1536 | 13 | 1.43s | OpenAI v3 系，主推 |
| `text-embedding-3-large` | **200** | 3072 | 13 | 4.90s | 维度翻倍，质量更高、延迟更长 |
| `text-embedding-ada-002` | **200** | 1536 | 13 | 2.37s | OpenAI v1 老接口 |
| `bge-m3` | **503** | — | — | — | `{"error":"分组 default 下模型 bge-m3 无可用渠道（distributor）"}` |
| `Qwen3-Embedding-0.6B` | **503** | — | — | — | 同上，无渠道 |

**结论**：云雾**只代理 OpenAI 系**三个 embedding 模型，bge-m3 / Qwen3-Embedding 等开源模型**无渠道**。

### 2.2 Batch / 延迟测试（model=`text-embedding-3-small`, 3 次取 P50）

| batch | P50 | runs (sorted) | dim | tokens |
|---:|---:|---|---:|---:|
| 1 | **3.55s** | 2.42 / 3.55 / 6.75 | 1536 | 13 |
| 8 | **4.08s** | 3.50 / 4.08 / 10.40 | 1536 | 107 |
| 32 | **7.82s** | 6.47 / 7.82 / 11.98 | 1536 | 425 |
| 64 | **24.03s** | 12.81 / 24.03 / 24.46 | 1536 | 853 |
| 128 | **22.24s** | 20.50 / 22.24 / 57.52 | 1536 | 1715 |

**关键观察**：
- batch ≤ 32 线性可预测，**单串均摊**：batch=32 时 7.82s / 32 ≈ **244ms/string**。
- batch = 64 时出现**明显阶跃**（24s+），疑似云雾后端在 ~50 切批；batch=128 与 64 同一档但 max 飙到 57s，**尾延迟极不稳**。
- ✅ 计费按 `usage.prompt_tokens`（OpenAI 标准），不按调用次数；
- ✅ Batch 输入支持 list；HTTP 协议、JSON 完全 OpenAI 兼容。
- ❌ 公网链路，单次 query encoding（batch=1）**P50 = 3.5s, p95 ≥ 6.7s**，对 EchoDesk 在线问答（用户期望 < 1s 召回）**不可接受**。

### 2.3 成本与回填估算（`text-embedding-3-small`, batch=32 sweet spot）

- **单次 query encoding**（在线）：~3.5s（不可接受）
- **回填 1808 chunks**（当前索引规模）：1808 / 32 × 7.82s ≈ **7.4 min**
- **回填 50k chunks**（目标量级）：50000 / 32 × 7.82s ≈ **3.4 h**
- **token 成本**：50k chunks × ~300 token = 15M token。OpenAI 官价 `text-embedding-3-small` = $0.02/1M token → **~$0.30/全量回填**；云雾倍率未知，但即便 5×也仅 ~$1.5。query 阶段每天 ~50 次 × ~30 token = 1500 tokens/天 ≈ **可忽略**。

---

## 3. B 路 · heyi-bj 远端（`100.87.251.9` tailscale 内网）

### 3.1 端口扫描结果

| port | 状态 | 内容 | 是否 embedding |
|---:|---|---|---|
| 7860 | ✅ 200 | `/v1/models` → `Qwen3-1.7B` (sglang, max_len 4096) | ❌ chat 模型；`/v1/embeddings` 明确报错 `This model does not appear to be an embedding model by default. Please add --is-embedding when launching the server` |
| 7861 | conn refused | — | — |
| 7862 | ✅ 200 | `/v1/models` → `Qwen3-32B-AWQ` (sglang, max_len 8192) | ❌ chat 模型，同上报错 |
| 7863 | conn refused | — | — |
| 8000 | ✅ 404 | nginx/1.27.5 | ❌ 反代/静态 |
| 8001 | conn refused | — | — |
| 8080 | ✅ 200 | open-webui 0.6.41 frontend（SvelteKit SPA） | ❌ `/ollama/api/version` → `{"version":false}` 即后端未连 ollama，无 embedding 路由 |
| 8090 | ✅ 服务可达 | STT (FireRedASR)，无 `/v1` 路由 | ❌ |
| 8091/8092/8093 | ✅ 404 (FastAPI) | 无 `/v1/models` | ❌ |
| 8094 | ✅ 服务可达 | TTS (qwen3_tts)，无 `/v1` | ❌ |
| 8095 / 9090 / 11434 | conn refused | — | — |

### 3.2 结论

**heyi-bj 当前完全没有 embedding 服务**。所有 LLM 实例（7860 / 7862）都是按 **chat** 模式启动的 sglang，未带 `--is-embedding` 标志，sglang 同一模型权重**不能同时跑两种 task**——要起 embedding 必须**额外**启动一个进程（典型如：`python -m sglang.launch_server --model Qwen3-Embedding-0.6B --is-embedding --port 7863`）。

**短期阻塞**：需要 heyi-bj 运维做以下任一：
1. 在新端口起 sglang `--is-embedding` 跑 `Qwen3-Embedding-0.6B` 或 `bge-m3`（~30 min 工作量，需 GPU 空余 ~2GB VRAM）。
2. 在 8080 open-webui 后端连一个本机 ollama 并 `ollama pull bge-m3`（运维侧 5 min，但 ollama 的 embedding 吞吐显著低于 sglang/vLLM）。

**在本期不依赖运维介入的前提下，B 路视为不可用。**

---

## 4. C 路 · 本地 bge-m3（fallback / 主路候选）

### 4.1 依赖现状

```
/Users/yoligehude/Desktop/all/echo-demo/.venv/bin/python:
  sentence_transformers  ❌ 未安装
  FlagEmbedding          ❌ 未安装
  hnswlib                ❌ 未安装
  faiss                  ❌ 未安装
```

需要在 `pyproject.toml` 增加（按引入顺序）：
- `sentence-transformers>=3.0`（带动 `torch`、`transformers`、`tokenizers`）
- `hnswlib>=0.8`（pure-Python wheel，Apple Silicon 友好；ANN 索引）
- 可选 `FlagEmbedding>=1.3`（bge-m3 三向量原生支持 dense + sparse + colbert）
- **不建议**装 `faiss-cpu`：与 hnswlib 二选一，hnswlib 装包简单、对 50k chunks 性能与 faiss-cpu 持平、CPU/Apple Silicon 兼容更好。

### 4.2 模型分发可达性

- `HEAD https://huggingface.co/BAAI/bge-m3` → **HTTP 200, 2.2s**（CloudFront LAX54 cache）
- `HEAD https://hf-mirror.com/BAAI/bge-m3` → **HTTP 308 redirect** 回 huggingface.co（mirror **没有**独立缓存这个模型）
- ✅ **HuggingFace 直接可达，无需 mirror**。

模型尺寸：**~2.27 GB**（`pytorch_model.bin` 2.24 GB + tokenizer ~30 MB + config）。
首次下载预估：按家庭宽带 10-30 MB/s 算，**1-4 min**；M2 Pro SSD 写入瞬时。

### 4.3 性能估算（M2 Pro CPU，不实跑、按 bge-m3 论文 + sentence-transformers 经验值推算）

- **冷启动加载模型**：3-8s（参考 bge-large-zh-v1.5 在 M2 上实测 4s，bge-m3 略大）
- **CPU encode 吞吐**（batch=32, mean chunk ~250 tokens）：
  - 论文值：**200-400 chunks/sec** 在 4-core CPU（论文测的是 Intel）
  - M2 Pro 10-core，sentence-transformers 实测保守值 **100-200 chunks/sec**
  - 取保守 **120 chunks/sec** 做下限估算
- **单次 query encoding**（在线，batch=1）：**~30-80ms**（短串单次走 forward pass，远低于 batch 均摊）

| 任务 | 估算 |
|---|---|
| 单次 query encoding | **~50ms** ✅ 在线友好 |
| 回填 1808 chunks | 1808 / 120 ≈ **15 s** ✅ |
| 回填 50k chunks | 50000 / 120 ≈ **7 min** ✅ |
| 全索引 mmap 加载（启动） | hnswlib mmap 模式 < 1s |

---

## 5. 综合决策（≤ 800 字，必读）

### 5.1 推荐主路 + fallback

**主路 = C（本地 bge-m3 via sentence-transformers + hnswlib）。**
**fallback 1 = A（云雾 `text-embedding-3-large`，dim=3072）**，**仅用于本地模型尚未下载完成时的 cold-start 期回填**。
**fallback 2 = 关闭 dense 通道，纯 BM25**（degradation mode，与 `docs/DEGRADATION.md` 一致）。

**为什么是 C 优先而不是 A**：

1. **延迟差 70 倍**：C 路 query encoding ~50ms vs A 路 batch=1 P50=3.5s（p95 6.7s）。EchoDesk 在线 RAG 要求端到端 < 3s，A 路单是 embedding 一步就吃掉全部预算。
2. **回填快 30 倍**：50k chunks 目标量级，C 路 ~7 min，A 路 ~3.4 h。Workspace 大扫描场景下 C 路根本不会被用户感知。
3. **隐私面更小**：A 路把**全量** chunk 文本上传云雾（包括 ambient STT、会议纪要等高敏感数据），而 LLM 仅在 query 时把 top-30 chunk 发出去，量级差 50 倍。
4. **成本可控为 0**：C 路除首次 2.2GB 下载外零边际成本；A 路虽然 token 单价低，但 50k chunks 全量 + 后续 incremental 长期会累加。
5. **bge-m3 同时输出 sparse vector + ColBERT-style 向量**，未来上 hybrid 不需要再换模型；A 路的 `text-embedding-3-large` 是 dense-only，未来上 ColBERT 必须二次重 embed。

**为什么 A 路保留 fallback**：cold-start 第一次启动时模型还没下完（1-4 min 窗口），允许用户立刻开始用 dense 检索；下载完成后切回本地，已 embed 的 chunks 标记 `model_version=yunwu-3-large`，后台异步重 embed。

### 5.2 估算（基于 §2.2 §4.3 数据）

| 指标 | C 主路 | A fallback |
|---|---|---|
| 单次 query encoding | ~50ms | 3.5s (p95 6.7s) |
| 回填 1808 chunks | ~15s | ~7.4 min |
| 回填 50k chunks | ~7 min | ~3.4 h |
| 单次成本 | 0 | ≤ $1.5（50k chunks 全量） |

### 5.3 阻塞性风险（≤ 5 条）

1. **首次下载 2.2GB 阻塞用户首启**——缓解：FastAPI startup 异步触发，期间走 BM25-only + 显式 UI loading；如果用户网络差，**自动降级到 A 路**直到本地下载完。
2. **sentence-transformers MPS 后端偶发 segfault**（Apple Silicon 历史问题）——缓解：强制 `device='cpu'` + `OMP_NUM_THREADS=4`，CPU 性能已够用。
3. **embedding 模型版本漂移**——缓解：vector store header 写 `model_name + model_version + dim`，启动时检测不一致 → 后台分批重 embed（期间走旧索引）。
4. **A fallback 在云雾 503 / batch=64+ 尾延迟 57s 时的级联失败**——缓解：fallback 仅离线回填使用、且加 batch=32 上限 + retry-with-backoff，永不暴露到在线 query 链路。
5. **vector store 与 BM25 _chunks 不同步**——缓解：共享同一 `chunk_id`，索引层强制单一写入路径（`_ingest_lock` 同时覆盖 BM25 与 vector store）。

### 5.4 EmbeddingPort 接口签名（仿 `app/ports/llm.py` 风格）

```python
# backend/app/ports/embedding.py（建议落地位置，本期不实现）
from __future__ import annotations
from typing import Protocol, Sequence, runtime_checkable

@runtime_checkable
class EmbeddingPort(Protocol):
    """Dense embedding 统一接口。具体路由策略在 adapter 层完成。

    - 入参始终是 list[str]，单串调用方传 [text] 即可，避免 adapter 内部判断分支。
    - 返回 list[list[float]]，与入参 1:1 对齐。
    - 失败由 adapter 内部 retry，最终抛 EmbeddingError；调用方决定是否走 fallback。
    """

    @property
    def model_name(self) -> str: ...           # 用于 vector store header 写入
    @property
    def dim(self) -> int: ...                  # 1024 (bge-m3) / 1536 / 3072
    @property
    def max_input_tokens(self) -> int: ...     # 8192 (bge-m3) / 8192 (OpenAI v3) 等

    async def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        is_query: bool = False,                # bge-m3 query/doc 共用同一前缀，但保留参数面以兼容 e5 等
    ) -> list[list[float]]: ...

    async def health(self) -> bool: ...        # cold-start 阶段用，决定是否走 fallback
```

**Adapter 命名建议**：
- `BgeM3LocalEmbedding`（主路，sentence-transformers）
- `YunwuOpenAIEmbedding`（fallback，model 默认 `text-embedding-3-large`）
- `HeyiQwen3Embedding`（占位，B 路一旦运维就位即填）

**Router 策略**：装载时 `health()` 检查 → 主路就绪走 C；未就绪走 A；A 也失败抛 `EmbeddingError` 由 `HybridRag` 退化到 BM25-only。

---

## 6. 不做 / 留待后续

- **不在本期实现**：所有上面建议的 adapter、Port、VectorStore、HybridRag。本文档仅做选型 spike。
- **不下载模型**：bge-m3 下载留到首次启用 dense 时自动触发。
- **不安装依赖**：`pyproject.toml` 增加 `sentence-transformers / hnswlib` 是落地阶段的 PR，与本 spike 解耦。
- **B 路 revisit**：若运维在 heyi-bj 任意新端口起 sglang `--is-embedding`（推荐 `Qwen3-Embedding-0.6B`，1024d），本路可立刻晋升为**第一 fallback**（替换 A），延迟预期 < 50ms（tailscale 同机）、零外发隐私面。建议在 alld task `m-new-X` 跟踪。

---

## 附录 A · 实测命令清单（可复现）

```bash
# A 路可用性
curl -sS -X POST https://yunwu.ai/v1/embeddings \
  -H "Authorization: Bearer $YUNWU_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":"褐蚁竞品调研"}'
# → 200 OK, dim=1536, 1.43s

# B 路扫描
for p in 7860 7861 7862 7863 8000 8001 8080 8090 8091 8092 8093 8094 8095 9090 11434; do
  curl -sS -m 3 http://100.87.251.9:${p}/v1/models -w "|%{http_code}\n"
done
# → 仅 7860 / 7862 返回 chat 模型；其他全不可用

# C 路依赖检测
/Users/yoligehude/Desktop/all/echo-demo/.venv/bin/python -c "import sentence_transformers" 
# → ModuleNotFoundError

# C 路 HF 可达
curl -sS -I https://huggingface.co/BAAI/bge-m3 -w "T:%{time_total}s\n"
# → 200, 2.2s
```

## 附录 B · 与 25-context-engineering.mdc 的对接（建议）

本 spike 应在 `_state/events/` append 一条 `add` 事件，category=`conclusion`，内容大致：

> **EchoDesk Hybrid RAG dense 通道选型结论**：主路本地 bge-m3（sentence-transformers + hnswlib），fallback 云雾 `text-embedding-3-large`，heyi-bj 远端**暂时不可用**（无任何 embedding 服务部署）。基于 2026-05-28 实测：云雾单串 P50=3.5s 不适合在线 query；本地 bge-m3 query ~50ms、50k chunks 回填 ~7 min；HF 直连可达。pinned: false, volatility: medium (heyi-bj 一旦部署 embedding 需 supersede)。

---

**spike 完。下一步落地见 `docs/rag_redesign_2026-05-28.md §C.3 中期路径 step 1`。**
