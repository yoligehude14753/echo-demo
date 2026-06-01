# RAG Hybrid Baseline · 2026-05-28（phase5-hybrid-rag）

把 EchoDesk 的 RAG 主链路从纯 BM25 升级为 **BM25 + bge-m3 dense + RRF fusion**
（`docs/rag_redesign_2026-05-28.md §C.3` 中期路径）。

本文档汇总落地版的索引规模、回填耗时、A/B 评测数据、延迟、风险与不达标项。

> 配套报告：[`docs/rag_eval_hybrid_2026-05-28.md`](rag_eval_hybrid_2026-05-28.md)（rag_eval.py 自动生成的 50-query 明细表）。

---

## 1. 索引规模

| 项 | 值 |
|---|---|
| 总 docs | 34 |
| 总 chunks (BM25) | 1942 |
| 总 vectors (dense) | 1942 |
| Embedding 模型 | `BAAI/bge-m3`（本地，dim=1024，cosine）|
| 向量索引引擎 | hnswlib（M=32，ef_construction=200，ef_query=50）|
| BM25 索引目录 | `~/.echodesk/rag_index/` |
| 向量索引目录 | `~/.echodesk/rag_vector_index/`（`index.bin` + `index.json` sidecar）|

数据源构成（按 source 维度）：
- workspace PDF/PPTX/CSV（用户授权目录）：8 + 多
- meeting（会议纪要 + 逐字稿）：多个 `meeting-*`
- ambient（环境录音转录，按日聚合）：`ambient-20260528` 1236 chunks

---

## 2. 冷启回填

```
脚本：scripts/backfill_dense_vectors.py
命令：~/.echodesk/source/backend/.venv/bin/python scripts/backfill_dense_vectors.py
模型：本地 bge-m3（sentence-transformers + torch 2.6.0）
设备：CPU（mps 在 spike §5.3 已知偶发 segfault，本期不启用）
```

| 指标 | 值 |
|---|---|
| 待回填 chunks | 1929（总 1942 - 已有 13） |
| 失败 batches | 0 |
| 写入 vectors | 1929 |
| 总耗时 | **308.2 s（≈ 5.1 min）** |
| 吞吐 | 6.3 items/s（batch=32） |
| 最终 `n_vectors` | 1942 |

batch 内延迟分布：从 1.2s（前段 PDF 短 chunk）到 17s（尾段 ambient 长 chunk），主要受 chunk 长度影响。

---

## 3. 50-query A/B 评测

工具：`scripts/rag_eval.py` · `tests/rag_eval/queries.yaml`（50 query × 4 category × 3 difficulty）

### 3.1 总分对比

| 指标 | baseline (BM25-only) | hybrid (BM25 + dense + RRF) | Δ |
|---|---|---|---|
| Recall@10 | 73.0% | 76.4% | **+3.4pp** |
| **Recall@30** | **74.5%** | **78.4%** | **+3.9pp** |
| MRR | 0.5364 | 0.5461 | +0.0097 |
| 零召回 (R@30=0) | 9 / 50 | 8 / 50 | -1 |
| online retrieval p50（含 dense + RRF） | — | **55 ms** | n/a |

### 3.2 按 category 分项

| category | n | baseline R@30 | hybrid R@30 | Δ |
|---|---|---|---|---|
| cross_doc | 10 | 91.7% | 91.7% | +0.0pp |
| summary | 10 | 100.0% | 100.0% | +0.0pp |
| technical_qa | 20 | 76.7% | **87.5%** | **+10.8pp** ▲ |
| time_range | 10 | 27.5% | 25.5% | -2.0pp |

### 3.3 按 difficulty 分项

| difficulty | n | baseline R@30 | hybrid R@30 | Δ |
|---|---|---|---|---|
| easy | 20 | 88.3% | 91.7% | +3.4pp |
| medium | 20 | 76.7% | 79.6% | +2.9pp |
| hard | 10 | 43.3% | 49.7% | +6.4pp |

---

## 4. 新通过 / 回退 query 明细

### 4.1 ▲ 新通过（baseline 0 → hybrid >0）

**q018 · technical_qa · easy** —— `'HY90 硬件清单'`
- baseline top10 没拉出 `csv-301bcacbee44`（CSV BOM 表）；hybrid r@30 = 1.00（rank 5）
- 根因：CSV 文件里有 GPU/CPU 型号但没出现 "HY90" 字样；BM25 完全靠 token 命中失败，dense 通过 "硬件清单" 语义相似拉出
- **这是 dense 通道的典型胜场**：query 与 doc 字面无重叠但语义相关

### 4.2 ▼ 回退（hybrid 比 baseline 差）

**q044 · time_range · hard** —— `'本周关于褐蚁产品的对话或资料'`
- baseline r@30 = 1.00（5/5 全中）；hybrid r@30 = 0.80（4/5，少了 `meeting-auto-1779953535`）
- 根因：dense 把语义更"褐蚁"的 PDF 段拉得更高，原本 BM25 排在 top-30 内的会议被挤出
- 影响有限：top-30 内仍包含 5 个 expected 中的 4 个，下游 prompt 渲染 80 chunks 时多半仍能利用

### 4.3 仍持续零召回（baseline + hybrid 都 R@30=0）

```
q010 (cross_doc/medium)
q041, q043, q045-049 (time_range/medium-hard)
```

共同特征：
- time_range 7/10 query 仍 0：典型形如「上周三的会议」「最近的电力 AI 资料」——
  需要**日期实体抽取 + 元数据过滤**，仅靠 BM25/dense 的语义相似度无法解决
- q010 / q043 / q049 等：expected_doc_ids 指向的内容**不在当前 34 doc 索引内**（标注阶段把"应该有"的文档列了出来，但用户实际工作区里那些 PDF 还没入库）

---

## 5. 在线延迟

直接调用 `HybridRag.query(q, top_k=50)`（绕过 fast LLM 分类、Tavily web search、main LLM 生成），实测：

| 指标 | 值 |
|---|---|
| 单次 p50 | **55 ms** |
| 单次 mean | 58 ms |
| 单次 max（5 次） | 69 ms |

构成（按调用 trace 估算）：
- BM25 BM25Okapi.get_scores 在 1942 chunks ≈ 10-20ms
- bge-m3 query encode（CPU，单串）≈ 30-50ms
- hnswlib knn_query（cosine）≈ 1-3ms
- RRF 融合 + metadata 补全 ≈ < 1ms

**首次 query 慢 ~5s**：bge-m3 模型加载（torch.load + tokenizer init）。后续 query 驻留内存复用，回到 50ms 量级（factory 启动期主动加载，所以用户首请求一般已经热）。

> 端到端 `/rag/ask` SSE 首帧 p50 ≈ 10s，但这个数被 fast LLM 分类（heyi-bj :7860 Qwen3-1.7B）+ Tavily search + main LLM TTFT 主导，与 hybrid 切换无关；baseline 同口径 p50 = 16.9s（hybrid 略快，但都不在本 PR 的关注 SLA 内）。

---

## 6. 不达标项与原因

用户给出的通过门槛与实际结果：

| 门槛 | 实际 | 是否达标 |
|---|---|---|
| 总体 Recall@30 ≥ 85% | **78.4%** | ❌ -6.6pp |
| time_range Recall@30 ≥ 70%（零召回 ≤ 3 条） | **25.5%**（零召回 7 条） | ❌ |
| 任何 category 回退 ≤ 5pp | time_range -2pp，余皆持平/上升 | ✅ |

### 为什么没到 85% / 70%

**根因 1：time_range 类查询本质需要"日期实体抽取 + metadata 过滤"层**

10 条 time_range query 中 7 条仍零召回（baseline 同样的 7 条）。这些 query 形如：

- "上周三的会议"
- "最近的电力 AI 资料"
- "今天我和谁讨论了 XX"

dense embedding 能理解 "今天/上周" 的**语义**，但无法把它**绑定到具体日期**进而过滤 chunk metadata 里的 `captured_at`。BM25 同样做不到——这是一类**与 retrieval 算法本身正交**的能力缺口。

→ 解决方案需要在 use_case 层加一个 `time_filter`：
  1. 先用 fast LLM 解析 query → `{date_from, date_to, granularity}` JSON
  2. 把 metadata.captured_at / metadata.kind 落入 filter
  3. 把 filtered candidates 喂给 BM25 + dense fusion

这超出 phase5-hybrid-rag 的范围，纳入 `docs/rag_redesign_2026-05-28.md §C.4` 后续 PR。

**根因 2：部分 expected_doc_ids 指向尚未入库的 doc**

q010 / q043 / q049 等 query 的 ground truth 引用了某些 PDF/会议，但这些文档**根本不在当前 34 doc 索引里**——用户标注时按"理想覆盖范围"列了出来。在不补语料的前提下任何 retrieval 算法都拉不出来。这部分的修复路径是扩大索引规模（用户原话："1000 对话 + 100 文件"——我们当前 34 doc / 1942 chunks，离目标还有距离）。

**根因 3：technical_qa 已经吃到大量增益（76.7% → 87.5%）**

这是 dense 应该贡献最大的区域，已经基本兑现。余下的 12.5% 失败案例多为：
- 缩写歧义（"HY90"、"K2.6" 等本就难辨）
- expected 含 ambient 长 doc——本来就被我们 doc-cap=12 削掉了

### 取舍说明

我们**没有**为了硬冲 85% 做下列任何一项：
- 把 RRF 权重偏向 dense（会让 cross_doc/summary 的 BM25 优势衰减）
- 把 doc-cap 取消（实测会让 ambient daily 一天 1236 chunks 霸榜整个 prompt 窗口）
- 在 retrieve_and_answer 加 ambient_penalty=0（同上）

3.9pp 的总体增益 + 10.8pp 的 technical_qa 增益 + 新通过 1 条零召回 + 在线延迟 55ms，这套数字相对于 BM25-only 是稳定且有意义的改进，且**没有引入 ≥5pp 的 category 回退**。

---

## 7. 风险与已知问题

1. **冷启首次 query 慢 ~5s**：bge-m3 模型加载 + torch initialize。后续驻留内存。
   - 缓解：`build_rag(settings)` 在 backend lifespan 早期执行，启动期就把模型 warm up；
     用户首请求来时已经热。
2. **torch >= 2.6 硬要求**：sentence-transformers >= 3.4 对 `torch.load` 安全策略收紧。
   旧 venv（torch 2.4.1）跑会抛 `ValueError: Due to a serious vulnerability issue in torch.load ...`，
   `EmbeddingRouter` 会自动落到 yunwu fallback；为保证主路工作，
   `~/.echodesk/source/backend/.venv/` 已升级至 `torch==2.6.0 torchaudio==2.6.0`。
3. **hnswlib 是 C 扩展**：M2 Pro/arm64 wheel 已 prebuilt；CI 环境如装非 arm64 会重编。
4. **dense 失败的 graceful degradation**：任何一步失败（依赖未装 / 模型加载失败 / encode 超时 / hnswlib 异常）→ HybridRag 自动单查询降级为纯 BM25 + log warning，不抛错。`factory.build_rag` 启动期同样：异常情况退回 `BM25Rag(settings)`。

---

## 8. 测试覆盖

- 新增 unit test：
  - `backend/tests/unit/test_vector_store.py` — 7 用例（add / search / delete / reload / replace / dim mismatch / compact / sidecar JSON）
  - `backend/tests/unit/test_hybrid_rag.py` — 5 用例（ingest+query / dense fail fallback / ingest dense fail / RRF math / dense-only hit promote）
- 全套测试通过：见 PR description 的 CI 表格

---

## 9. 后续工作

按优先级：

1. **time_range 类元数据过滤层**（直接闭合 -10pp 的 category 缺口；估算 +6-15pp 总体 R@30）
2. **回填脚本 idempotent + incremental**（用户重启后跑一次自动补齐增量）
3. **vector store compact 优化**：>10k chunks 时全量重建 1s+，改成 dirty-region 重建
4. **bge-m3 sparse + ColBERT 通道**：FlagEmbedding 已装；中期 PR 用 sparse 做 keyword 类 query 的辅助 ranker（spike §5 提议）
5. **expand 语料**：用户目标 1000 对话 + 100 文件，当前 34 doc 离目标差 3-5×；
   workspace_scanner + ambient 会随时间累积，但应该补一轮主动 ingest（用户 docs 目录）

---

## 10. 相关文档

- 总规划：[`docs/rag_redesign_2026-05-28.md`](rag_redesign_2026-05-28.md) §C.3
- Embedding spike：[`docs/rag_embedding_spike_2026-05-28.md`](rag_embedding_spike_2026-05-28.md)
- BM25 baseline 详表：[`docs/rag_eval_baseline_2026-05-28.md`](rag_eval_baseline_2026-05-28.md)
- 本次 hybrid 详表：[`docs/rag_eval_hybrid_2026-05-28.md`](rag_eval_hybrid_2026-05-28.md)
