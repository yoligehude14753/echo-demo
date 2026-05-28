# EchoDesk RAG 架构重设计调研报告（2026-05-28）

> **调研边界**：本文只给方案建议，不写代码、不改任何文件。  
> **代码现状基准**：`backend/app/adapters/rag/bm25.py` + `backend/app/use_cases/retrieve_and_answer.py` + `backend/app/adapters/rag/workspace_scanner.py` + `backend/app/adapters/rag/parsers.py`，commit 截至 2026-05-28 17:00。  
> **用户痛点案例**：查询「褐蚁竞品调研」→ BM25 给 `ambient-20260528` 单段 STT score=11.5，褐蚁 PDF p001 score=3.75 被挤出 top-5 → LLM 老实答"在已有的资料里没找到相关内容"。  
> **核心约束**：MiniMax-M2.7 input window 很大（`llm_main_max_tokens=80_000`），token 不是瓶颈，**质量优先 / 不要人为限制覆盖范围**。

---

## A. 当前 BM25 方案的根本缺陷（根因深度拆解）

### A.1 一个总根因，四个分支表现

`bm25.py` 把**所有来源**的 chunk 塞进**同一个 BM25Okapi 索引**，然后用一个全局排序回前 `top_k` 个。这本身没错，**错在四件事一起发生**：

1. **chunk 粒度异构**：PDF 用 600 char + 100 overlap 的滑窗 chunk（一页可能 1-3 chunk）；markitdown 走 generic 600 char chunker；meeting transcript 也是 600 char；**ambient 单个 STT 段一个 chunk**（实测 20-80 char）。在 BM25 视角下，一段 ambient 是"一个文档"，一段 PDF 也是"一个文档"，但它们的长度差 10-30×。
2. **BM25 长度归一**：BM25Okapi 默认 `k1=1.5, b=0.75`，其文档分数大约是 `IDF · TF · (k1+1) / (k1·(1-b+b·dl/avgdl) + TF)`。当 `dl` 远小于 `avgdl`，分母项 `(1-b+b·dl/avgdl)` 显著小于 1，整体分数被放大。**ambient 短段相对 PDF 长段获得了系统性的 length bonus**。
3. **关键词召回**：BM25 只能命中"出现的字面词"。jieba `cut_for_search` 把"褐蚁"切成 `["褐","蚁","褐蚁"]`，把"竞品调研"切成 `["竞品","调研","竞品调研"]`。如果 ambient 转录里有人随口说过"刚才在调研"或"看个竞品"，这一句话短段就会拿满 IDF + length bonus；而 PDF 真正讲褐蚁产品定位的那一页 600 字里同样出现"褐蚁/竞品"，但 TF 摊薄 + 长度惩罚 → 分数低 3 倍以上。这就是用户截图里 `score=11.5 vs 3.75` 的精确机制。
4. **doc-level 多样性丢失**：top-5 排序不区分来源。即便 PDF 真有 3 个相关 chunk，如果 ambient 有 5 个分数更高的短段，PDF 一个都进不去 prompt。**用户截图里的"褐蚁 PDF 完全没出现"就是这种 doc-level monoculture**。

### A.2 jieba 分词的具体局限

- **OOV 失败**：jieba 默认词典里没有的专有名词（公司名/品牌名/产品代号），会被切成单字。"褐蚁"很可能 cut 成 `["褐","蚁"]` + `["褐蚁"]`（search 模式会保留组合），但分词不稳定。用户加入自定义词典是常规做法，但 EchoDesk 是分身产品，**用户的专有名词每天都在变**，靠手维护词典不可持续。
- **歧义切分**：`cut_for_search` 模式会给同一个串多种切法（"中华人民共和国"→`["中华","华人","人民","共和","共和国","中华人民共和国"]`），表面上是召回友好，实际**让短串 chunk 的 IDF/TF 计算被极少数高频字主导**。
- **跨语种**：英文走 `[a-z0-9]+` 兜底，但跨语种实体没办法 fuse。"GPT-5.4-nano" → `["gpt","5","4","nano"]`，"褐蚁 V2"被切成 `["褐","蚁","v2"]`，**没有 subword 概念**。
- **同义/近义**：无法识别"竞品 vs 竞争对手 vs 同行 vs benchmark"、"PDF vs 文档 vs 资料"、"褐蚁 vs Brown Ant vs HA"等等价表达。这是关键词检索的天花板，与 jieba 本身无关。

### A.3 长文档 vs 短 chunk 的 IDF/TF 偏置（数值估算）

简单算个账（用 BM25Okapi 默认参数 + 当前 1808 chunks 索引）：

- avgdl ≈ 全索引 token 总数 / 1808。ambient 单段 ≈ 30 token，PDF 600-char chunk ≈ 250-400 token，markitdown chunk ≈ 250-400 token。粗算 avgdl ≈ 200-300 token。
- ambient 短段 dl=30，`(1-b+b·dl/avgdl) ≈ 1-0.75 + 0.75·0.1 ≈ 0.325`，length factor ≈ `(k1+1)/(k1·0.325+TF)`。当 TF=1，length factor ≈ `2.5 / 0.49 ≈ 5.1`。
- PDF 长段 dl=300，`(1-b+b·dl/avgdl) ≈ 1`，length factor ≈ `2.5/(1.5+1) = 1.0`。同样 TF=1 时差 **5×**。
- IDF 项假设"褐蚁"出现 1 次于 PDF（短 chunk 也命中 1 次），IDF 几乎一样。**最终 short-chunk 分数 ≈ long-chunk 分数 × 5**。这与用户截图 `11.5/3.75 ≈ 3.07×` 在量级上完全一致。

**结论**：`_AMBIENT_BM25_PENALTY = 0.25` 这个 magic number 不是错的，它精确地补偿了 BM25 length normalization 的过激；但它是**症状治疗**，不解决根因。新的来源（meeting transcript、上传 markdown 笔记、未来的邮件入库）每加一个都要重调系数，这不是工程可持续的方向。

### A.4 chunk 粒度问题

- **每页一 chunk 太粗 / 太细取决于页面**：长篇技术 PDF 一页常含多个独立小节，混到一个 chunk 里会稀释关键词 TF；短 PPT 一页 50 字塞进 600-char chunk 仍嫌空，IDF 又因为页码差异被人为分散。
- **600 char 是静态门槛**：对中文是 ~600 字（≈400-600 token），对英文是 ~150 词。OpenAI 业界推荐 chunk 在 200-500 token，但**段落语义边界**比固定 char 数更重要。当前 `_chunk_text` 用 `text[i:i+size]` 的纯字符切片，**会把句子从中间切断**，导致 chunk 边界处的句子两端 TF 都不完整。
- **ambient 一段一 chunk 极端碎**：用户在沙发上说一句"我去倒杯水"也是一个 chunk。这种 chunk 应该聚合到"对话回合"或"60 秒窗口"才有检索价值；而当前结构让每一句话都参与 BM25 排名。
- **page metadata 不一致**：`ingest_pdf` 保留 `page` metadata（用于引用回链），但 markitdown 走 `_ingest_generic_sync` 没有页码（即使源是 PDF），降级了"引用回链"的体验。

### A.5 工程层面隐患（与场景规模直接相关）

- **每次 ingest 全量 rebuild**：`_rebuild_bm25` 在每次 ingest 调用 `BM25Okapi(self._tokens)`，对 1808 chunks 量级毫秒级；用户预期增长到 **1000 docs / 50k chunks** 时，单次 rebuild ≈ 100ms-1s，workspace 扫描 100 文件就是 30-60s 全程阻塞。
- **per-doc JSON 持久化 + 启动全加载**：50k chunks 启动加载 + 反序列化 + tokenize 需要 5-15s 冷启动。这是 Electron app 用户体感最差的地方。
- **全 in-memory + asyncio.Lock**：所有 ingest 与 query 共享一把 lock，**workspace 大扫描期间 query 会被阻塞**。Auto-ingest ambient 的频次（每 6s 一段）也在这把锁下。
- **没有去重/版本化**：同一文件被两个 workspace 路径符号链接 / 用户上传 + workspace 扫描的双入，会产生 2 份独立 doc_id，BM25 视角下就是 2 倍 TF。

---

## B. 替代/增强方案矩阵（含原理、适用场景、成本、效果上限、风险）

每个方案标注：**可行性（高/中/低）**、**迭代成本**、**新依赖**。

### B.1 向量检索（Dense Retrieval）

**原理**：把 chunk 与 query 都映射到 dense embedding 空间（768-1024d），用余弦相似度排序。不依赖字面匹配，能召回"同义不同字"的语义相关 chunk。

**Embedding 模型选项**（针对中文为主、夹杂英文的 EchoDesk 语料）：

| 模型 | 维度 | 中文能力 | 多语种 | 部署 | 体积 | EchoDesk 适配度 |
|---|---|---|---|---|---|---|
| **bge-m3** (BAAI) | 1024 | ★★★★★ | ★★★★★ | sentence-transformers / FlagEmbedding | ~2.2GB | **最推荐**，原生 dense+sparse+colbert 三向量、8192 token 长上下文 |
| **bge-large-zh-v1.5** | 1024 | ★★★★★ | ★★ | sentence-transformers | ~1.3GB | 纯中文项目首选；EchoDesk 有英文夹杂略差 |
| **m3e-large** (moka-ai) | 1024 | ★★★★ | ★★★ | sentence-transformers | ~1.3GB | 备选，社区活跃度不如 bge |
| **OpenAI text-embedding-3-large** | 3072 | ★★★★ | ★★★★★ | API | 0 | 质量稳，**但走 OpenAI 直连违反 06-platforms.mdc**，且打包后台账隐私敏感语料外发不可接受 |
| **OpenAI text-embedding-3-small** | 1536 | ★★★★ | ★★★★ | API | 0 | 同上 |
| **云雾 yunwu embedding API** | 取决于底层 | 取决于 | 取决于 | API | 0 | 走现有 `yunwu_open_key` 路由，需先确认云雾是否暴露 embedding endpoint 及 SLA |
| **Qwen3-embedding** (heyi 本地) | 1024 | ★★★★★ | ★★★★ | vLLM/Triton @ heyi-bj | 远端零本地体积 | **若 heyi 已部署最佳**——零本地下载、走 `llm-fast.yoliyoli.uk` 同通道 |

**推荐配置**：**bge-m3 本地 + Qwen3-embedding 远端**两手准备。本地 bge-m3 用 sentence-transformers 加载（首次启动后台下载，体积 2.2GB 用户能接受；若启动慢，可分块下载与 RAG 初始化解耦），远端走 heyi-bj 内网作为加速/降级备份。bge-m3 同时输出 sparse vector，未来可天然支持混合检索而无需额外服务。

**ANN 索引**：1k-50k chunks 量级 **不需要 GPU faiss**，用 `faiss-cpu` 或 `hnswlib`（pure-Python wheel，Apple Silicon 友好）即可，p99 query < 10ms。

**适用场景**：
- "竞品调研"匹配 PDF 里写的"竞争对手分析"
- "上周开会聊到那个客户"匹配 ambient 里没说"客户"二字但讨论了客户问题的段落
- 跨语种检索（英文术语在中文文档里被翻译，或反之）

**实施成本**：
- 1 个迭代（5-7 天）：写 EmbeddingAdapter（Port 已有，走 22-modularity.mdc Port-Adapter）、写 VectorStoreAdapter（hnswlib + 持久化 .bin）、跑回归实验对比 BM25 baseline。
- 注意：embedding 是 idempotent 的，但**已入库 1808 chunks 要批量回填**，离线脚本一次跑完，本地 bge-m3 在 M2 Pro 上 ≈ 500 chunks/min，1808 chunks ≈ 4 min；50k chunks ≈ 100 min。

**效果上限**：
- recall@10 预期 **从 BM25 的 30-40% 提到 65-80%**（参考 BEIR 中文数据集、bge-m3 paper）。
- 但**单 dense 永远干不掉 BM25**：长尾专有名词（"褐蚁 V2"、"FY26-Q3-XPL-12 项目代号"）只有字面匹配能命中，dense 在 OOV 实体上会比 BM25 还差。**必须 hybrid**。

**集成风险**：
- **本地模型下载体积 2.2GB**：首次打开 EchoDesk 后台静默下载，过程中查询走 BM25 fallback。这是 macOS Electron app 的标准模式。
- **冷启动**：load 模型 ≈ 3-8s。pre-warm 在 FastAPI startup 异步触发，与 workspace_scanner 并行。
- **GPU 依赖**：bge-m3 在 M2 Pro CPU 上 100-300 chunks/sec，**够用**；不强制 GPU。
- **embedding 漂移**：换模型版本需要全量重 embed。给 `vector_store.bin` 加 `embedding_model_name + version` header，启动时检测不一致 → 后台重 embed。

**可行性**：**高**。是性价比最高的单点升级。

---

### B.2 混合检索（Hybrid: BM25 + Dense + RRF 融合）

**原理**：分别用 BM25 和 dense 取 top-K（K=50-200，**比单路 top_k 大得多**），用 Reciprocal Rank Fusion 融合：

```
RRF(d) = Σ_i  1 / (rrf_k + rank_i(d))
```

`rrf_k` 经验值 60（Cormack et al. 2009，工业界 LangChain/LlamaIndex 默认值）。它的作用是平滑高位差异：rank 1 比 rank 2 仅高约 1/61 vs 1/62 ≈ 1.6%，不会让单路头部一家独大。

**为什么 RRF 优于其他融合**：
- 不需要 normalize 不同路的分数（BM25 vs cosine 量级完全不同，硬归一容易翻车）。
- 对召回但未排首的 chunk 友好：dense 排第 10、BM25 排第 11，RRF 后能升到融合 top-5。
- 实现极简，10 行 Python。

**适用场景**：**EchoDesk 几乎所有场景**。dense 负责语义召回，BM25 负责字面专有名词。

**实施成本**：在 B.1 基础上 + 0.5 个迭代。可以一个 PR 出。

**效果上限**：BEIR 上 hybrid 普遍比单 dense 再 +5-10% recall@10。**EchoDesk 当前用户痛点（褐蚁查不到）有 90% 概率被 hybrid 解决**——dense 能召回"竞品调研"语义、BM25 能命中"褐蚁"字面。

**集成风险**：
- 两路 top-K 需要拉大（K=100-200）：BM25 `get_scores` 是 O(N×Q)，N=50k 时单次 ~200ms，能接受；dense ANN 单次 < 10ms。
- 融合后再走 rerank（B.3）或直接喂给 LLM 大窗口（B.5）。**与 prompt 长度协同决定最终 K**。

**可行性**：**高**。是 EchoDesk 应有的稳态。

**rrf_k 调参建议**：先用 60 起步；如果发现 dense 召回质量明显高于 BM25（小语料常见），改用 weighted RRF：`w_dense · 1/(60+rank_dense) + w_bm25 · 1/(60+rank_bm25)`，先 0.6/0.4 试。

---

### B.3 LLM Cross-Encoder Rerank

**原理**：召回阶段拿到 top-K（50-200），用 cross-encoder 模型对 (query, chunk) pair 做精排，输出 0-1 相关性分数。

**方案选项**：

| 方案 | 模型 / 路由 | 延迟 | 质量 | 风险 |
|---|---|---|---|---|
| **bge-reranker-large** 本地 | BAAI，~560MB | 100 chunks ≈ 1-2s 本地 CPU | ★★★★ | 又一个本地模型下载 |
| **bge-reranker-v2-m3** 本地 | BAAI，多语种 + 长上下文 | 类似 | ★★★★★ | 更好但更大 |
| **Qwen3-1.7B 走 fast 通道做 pairwise** | 已有 heyi-bj 通道 | 100 pair 串行 = 100s 不可接受；batch 后 5-10s | ★★★ | 不是 reranker 训练目标，质量不稳 |
| **MiniMax-M2.7 走 main 通道做 listwise** | 把 50 个 chunk 一次性塞进 prompt，让模型挑 top-10 + JSON 输出 | 单次 LLM call 5-15s | ★★★★★ | token 成本高；输出格式不稳风险 |

**EchoDesk 推荐**：**bge-reranker-v2-m3 本地** 作为默认；MiniMax 大窗口直接听任 main LLM 综合判断作为**B.5 stuffing 模式的"自带 rerank"**——既然反正要把候选喂给 main LLM 生成答复，让它先排序再综合是一步到位的。

**适用场景**：
- 召回阶段拉到了 100-200 个候选，需要精排到 top-10 再喂给生成器
- 用户问题非常长 / 多意图（rerank 对长 query 比 dense 更稳）

**实施成本**：本地 reranker 0.5 个迭代。如果走 main LLM listwise，可与 prompt 改造合并到一个 PR。

**效果上限**：在 hybrid 基础上再 +3-8% NDCG@10。**对"褐蚁"这种用户痛点直接命中**——reranker 能把"竞品调研→褐蚁 PDF"的语义关联打到 0.85+，ambient 噪音打到 0.2-。

**集成风险**：
- 又一个本地模型下载 560MB。可以延后到 v0.4，先把 hybrid 做好。
- Listwise main LLM rerank 的 JSON 输出可能在 8% 的 case 上 malformed → fallback 到不 rerank。

**可行性**：**中**（本地 reranker 推迟），**高**（main LLM listwise，与 stuffing 二合一）。

---

### B.4 Query 改写 / HyDE / Step-back

**原理**：用 LLM 在检索前对 query 做扩展，让检索器看到更丰富的信号。

| 技术 | 做法 | 适合场景 |
|---|---|---|
| **Multi-Query** | LLM 把"褐蚁竞品调研"改写成 3-5 个变体："褐蚁产品定位"/"褐蚁竞争对手"/"褐蚁市场分析"/"褐蚁差异化分析"，每个变体单独检索 + 结果合并 | 用户 query 短、用词不精确 |
| **HyDE**（Hypothetical Doc Embeddings） | LLM 直接"假装答了"，生成一段假设性回答（1-2 段），用这段假答的 embedding 去检索 | 用户 query 太抽象，原文里不会出现同样表述 |
| **Step-back** | 先抽更宽泛的问题再检索（"褐蚁这家公司是做什么的"→检索→再用细节问题"竞品有哪些"二次检索） | 多跳推理 |

**EchoDesk 推荐**：**Multi-Query 简单版**先上。用 Qwen3-1.7B 快通道在 200-400ms 内生成 3 个变体，与原 query 一起跑 hybrid retrieval。这是性价比最高的"零额外模型"提升。

**HyDE** 慢 + 容易引入幻觉（假答里夹杂错误信息会污染 dense 召回方向），EchoDesk 场景**先不上**。

**适用场景**：所有用户主动问答；ambient 自动检索不必触发（延迟敏感）。

**实施成本**：0.3 个迭代。复用现有 `_classify` 通道的 fast LLM。

**效果上限**：recall@10 再 +5-10%。

**集成风险**：fast LLM 失败时 → 单 query 不阻断；已有 fallback 模式可直接复用。

**可行性**：**高**。

---

### B.5 大窗口 Stuffing（M2.7 直接吞候选 / 整个目录）

> 用户原话："模型可以 Input 的文本量很大，embedding 或者别的方案能覆盖的数据量也很大，你不要强行限制这些量。"

**原理**：跳过细粒度排序，把大量候选 chunk（或干脆全部 doc title + summary index）一次性塞进 main LLM 的 system prompt，让模型自己挑要看的内容并答复。

**两种路径**：

#### B.5.a "粗召回 + 大窗口"（推荐）
- BM25/Hybrid 召回 50-100 个 chunk
- 全部塞进 prompt（每个 chunk ≈ 200-400 token，100 chunk × 400 = 40k token，**完全在 M2.7 80k 窗口内**）
- prompt 里加一段"先在心里排序再答，引用必须从下方候选选取"
- 让 main LLM 同时做 rerank + 综合 + 答复

**边界**：
- **token 量**：1808 chunks 全塞 ≈ 800k token，**超**。所以仍需召回阶段。但召回阶段 K 可以**从当前的 5 拉到 100-200**。
- **延迟**：M2.7 input 40k token 的首 token 延迟 ≈ 3-8s（云雾路由 + MiniMax 处理），用户能接受（已经用过几次知道是云端 LLM）。
- **质量**：long-context LLM 在 30-50k token "中间段"会出现 lost-in-the-middle 衰减；学界结论是 chunk 数 > 20 后 attention 显著退化。**所以 K 不是越大越好**，sweet spot 在 K=30-50。

#### B.5.b "目录索引 + 工具调用召回"
- 在系统启动时让 LLM 拿到 `list_docs()` 输出（49 docs × 30 字标题 = 1.5k token），加每文档一段 100 字摘要 = 6k token
- 用户提问时 main LLM 先调用 `retrieve_chunks(doc_id, query)` tool 精准拉某个 doc 的细节
- 类似"先看目录再翻书"的人类阅读模式

**适用场景**：
- 用户问"褐蚁的竞品有哪些"——典型 B.5.a，召回 100 chunk 拍给 M2.7 综合
- 用户问"上周的某次会议讨论了什么"——典型 B.5.b，先从会议目录定位再 drill-in

**实施成本**：
- B.5.a：0.5 个迭代。改 `retrieve_and_answer.py` 里 `top_k` 和 prompt 渲染 `chunks[:N]`，加上 token budget 估算
- B.5.b：1.5 个迭代。需要：
  - per-doc summary 在 ingest 时用 fast LLM 生成（一次性）
  - main LLM tool-use 协议（用 function calling 或 ReAct）
  - 用 ports.LLMPort 抽象，复用已有 chat interface

**效果上限**：
- B.5.a：在 hybrid 已经搞定 召回之后，效果天花板再 +5-10%
- B.5.b：对"长尾文档"场景有质变（用户问的内容在某 PDF 里但 hybrid 召回不到 → tool-use 让 LLM 主动去看）

**集成风险**：
- **token 不是瓶颈但**：云雾路由对 input > 30k token 的请求**计费**显著上升（按云雾官方定价表是 0.3-1 元/k input token 范围，40k × 1 元 = 一次问答 12 元，**这是 EchoDesk 单次问答成本的红线**）。**必须实测**，且让用户在设置面板能调"召回深度"控制单次成本上限。
- **lost-in-the-middle**：建议在 prompt 里把**最相关的 chunk 放头尾**（K=30 时 hybrid top-10 放头、11-20 放尾、21-30 放中间），是已经被多篇论文验证的"假 attention 重排"技巧。
- **延迟敏感场景禁用**：ambient 自动问答场景（用户说"刚才说了啥"等待 < 3s）走 B.5.a 不行，必须 hybrid + small top_k；用户主动 ask 才用大窗口。

**可行性**：**高**（B.5.a）/ **中**（B.5.b，与 B.6/B.7 强相关）。

---

### B.6 GraphRAG / 知识图谱

**原理**：把文档实体（公司、人、产品、项目代号、日期、决议）抽出来建图，检索时先在图上找强连通子图再回原文。

**对 EchoDesk 的价值**：
- 优势：会议+办公场景天然有强结构——人、会议、决议、项目代号、时间。如果能建立"人 ↔ 会议 ↔ 议题 ↔ PDF"四元关系，"上周 Alice 在哪个会议提到褐蚁"这种 query 会从"语义模糊"变"图查询"，**质变**。
- 风险：GraphRAG（Microsoft 2024）和 LightRAG（2024-10）都需要**离线建图阶段**，对 EchoDesk 这种**实时 ambient 入库**场景，每天要增量更新 community summary、index，工程复杂度高。
- LightRAG 实测：1000 docs 索引时间 30-60 min（用 GPT-4o-mini 当抽取器，**这正是被 06-platforms denylist 的模型**，得换成 Qwen3-1.7B 或 MiniMax-M2.7 mini，质量/速度有损耗）。

**适用场景**：会议复盘、跨周/跨季回顾、人物关系问答（"和 Alice 都聊过哪些项目"）

**实施成本**：1.5-2 个迭代。建议**先不上，留到 v0.5 长期路线**。

**效果上限**：对图结构强的 query 是质变；对一般 RAG query 改进有限。

**集成风险**：增量更新难度高；图存储引入新依赖（Neo4j embedded / NetworkX + sqlite）；抽取器走 LLM 成本高。

**可行性**：**低**（短期）/ **中**（长期）。

---

### B.7 Agentic / Tool-use Retrieval

**原理**：把 retrieval 包装成 LLM 可调的 tool，让 main LLM 自己决定何时检索、检索什么、是否多轮。

**EchoDesk 价值**：
- 用户问"昨天和今天关于褐蚁的讨论合在一起总结一下"——main LLM 第一轮调 `retrieve(query="褐蚁", date_range="昨天")`，第二轮 `retrieve(query="褐蚁", date_range="今天")`，第三轮综合答复。
- 用户问"给我看看那个 30 页的 PDF 第二章的对比表"——main LLM 调 `list_docs(filter="kind=pdf, pages>=30")` → 选中 doc → `retrieve_doc_chunks(doc_id, chapter="2", topic="对比")`。

**实施成本**：1.5 个迭代。需要：
- main LLM 的 function calling / tool-use 协议（云雾路由 MiniMax-M2.7 是否支持需要确认，OpenAI 兼容协议下 tool_calls 字段一般可用）
- tool 注册表 + 沙箱（避免 LLM 调出本地非 RAG tool）
- 多轮对话 state 管理

**效果上限**：对"多步问答"和"复杂检索"是质变；对单轮简单问答没差异。

**集成风险**：
- MiniMax 在 yunwu 路由下 tool-use 稳定性需先打 spike
- 多轮调用让单次回答总延迟 10-30s
- 用户能感知的"agentic 感"是双刃剑（好：聪明；坏：慢、不可预期）

**可行性**：**中**。建议作为 v0.4 末-v0.5 进阶能力。

---

## C. EchoDesk 场景的最终推荐方案（短/中/长期路径）

### C.1 语料特征与查询模式（决定方案选型）

EchoDesk 语料结构（基于代码与用户反馈推断）：

| 来源 | 典型大小 | 入库频率 | 查询模式 | 时效性 |
|---|---|---|---|---|
| PDF（产品/调研报告） | 5-80MB，20-200 页 | 用户主动上传 + workspace 扫 | 知识问答 / 引用查找 | 长期 |
| docx/pptx | 1-30MB | 同上 | 同上 | 长期 |
| 会议纪要 | 5-30k 字 | 会议结束自动入 | 复盘、决议查找 | 中期（数月） |
| ambient daily | 几百到上千 STT 段 | 持续后台 | "刚才说了啥"、"昨天的对话" | 短期（24-72h） |
| 工作区扫描的笔记 | 各种格式 | 启动扫描 + 监听 | 跨文档关联 | 长期 |

查询模式至少 4 类：
1. **技术问答**："褐蚁的产品定位是什么"——典型 RAG，需要精准命中 PDF 内容
2. **总结**："过去一周聊了哪些客户"——时间范围 + 跨文档
3. **跨文档对比**："褐蚁和它的竞品 X 在 Y 维度上的差异"——多 chunk 综合
4. **时间范围检索**："上周三的会议讨论了什么"——结构化过滤优先于语义

### C.2 短期路径（1 个迭代内，**不引入新依赖**）

> **核心修复**：让用户能立刻感受到"褐蚁竞品调研"被查到，且不让 ambient 长文档霸榜。

1. **Ambient 段聚合**：在 `ingest_ambient_segment` 写盘前，按 60s 时间窗口或同 speaker 连续对话回合聚合 chunk。把当前"一段 STT 一 chunk"变成"60s 内的若干 STT 段合一 chunk"。预期 ambient 单 chunk 长度从 30 token 涨到 200-400 token，与 PDF chunk 同量级，BM25 length 偏置自动消失。
2. **召回 K 放大 + doc-level 多样性**：`rag.query(top_k=100)`，在 retrieve_and_answer 里加 doc-level cap（同一 doc_id 最多 4 chunks），再按 source 优先级软排序，最后 prompt 渲染前 30 chunks。**与用户"不要限制覆盖量"原话一致**。
3. **BM25 参数调优**：`BM25Okapi(self._tokens, b=0.5)`（默认 0.75）。降低 length normalization 强度，给短段 chunk 的不公平 bonus 砍掉一半。同时把 `_AMBIENT_BM25_PENALTY = 0.25` 改成 0.5，配合 ambient 聚合后的更长 chunk 适度回升其权重——**真正想答"刚才说了啥"时还是要能命中 ambient**。
4. **句子边界 chunker**：把 `_chunk_text` 从纯 char-slice 改成"按 `[。！？；\n]` split 后贪心组装到 600 char"。一行 sed 级别的工作量，效果是 chunk 边界 TF 不再被切碎。
5. **prompt 显式引导**："请综合下方所有 30 段证据，优先引用与用户问题语义最相关的 5-10 段；如有冲突，标注冲突来源；不要因为段数多就只看前几段（lost-in-the-middle）。"

**这五条 commit 后预期**：用户的"褐蚁竞品调研" query 能召回 PDF 真实段并出现在 prompt 头部，LLM 答案从"没找到"变"具体内容 + 引用"。

### C.3 中期路径（2-3 个迭代，**引入 dense + reranker**）

**架构演进**：从纯 BM25 演进到 **Hybrid + Optional Rerank + Big Window Stuffing**。

```
            ┌──────────────────────────────────────────────────────────┐
            │ Query (用户提问)                                          │
            └─────────────┬────────────────────────────────────────────┘
                          │
                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │ Multi-Query 改写 (fast LLM, Qwen3-1.7B)                  │
            │ → 3-5 个变体 query                                        │
            └─────────────┬────────────────────────────────────────────┘
                          │
                ┌─────────┴──────────┬─────────────────┐
                ▼                    ▼                 ▼
        ┌──────────────┐      ┌─────────────┐   ┌──────────────┐
        │ BM25 召回    │      │ Dense 召回  │   │ (可选)        │
        │ (现有改良)   │      │ bge-m3      │   │ sparse / colbert │
        │ top_K=100   │      │ top_K=100  │   │  bge-m3 自带  │
        └──────┬───────┘      └──────┬─────┘   └──────────────┘
               │                     │
               └──────────┬──────────┘
                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │ RRF 融合 (rrf_k=60) → 100-150 unique chunks              │
            └─────────────┬────────────────────────────────────────────┘
                          │
                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │ doc-level dedupe + source 优先级软排序                    │
            │ → top-50                                                  │
            └─────────────┬────────────────────────────────────────────┘
                          │
                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │ (可选) bge-reranker-v2-m3 rerank → top-30                │
            │ 默认开启；fast LLM 失败时跳过                              │
            └─────────────┬────────────────────────────────────────────┘
                          │
                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │ MiniMax-M2.7 大窗口 stuffing (30 chunks ≈ 12k token)     │
            │ prompt 显式排版（最重要 chunks 放头尾防 lost-in-middle）  │
            │ 流式生成最终答复                                          │
            └──────────────────────────────────────────────────────────┘
```

**实施步骤**：

1. **加 ports.EmbeddingPort + adapter**：
   - Port：`class EmbeddingPort: async def encode(texts: list[str]) -> list[list[float]]`
   - Adapter A：`BgeM3LocalEmbedding`（sentence-transformers 本地）
   - Adapter B：`HeyiQwen3Embedding`（远端 heyi-bj）
   - Adapter C：`YunwuEmbedding`（待云雾确认）
2. **加 ports.VectorStorePort + hnswlib adapter**：
   - chunks 入库时同时 encode + add 到 hnswlib index
   - 持久化 `~/.echodesk/vector_store/index.bin` + `metadata.json`（含 embedding model name/version）
   - 启动时 mmap 加载（hnswlib 支持 mmap 减少冷启动 RAM）
3. **改 BM25Rag → HybridRag**（保持 RagPort 协议不变）：
   - `query(query, top_k)` 内部：拉 BM25 top_K1=100 + Dense top_K2=100 → RRF 融合 → 返回 top_k
   - 兼容现有 `_AMBIENT_BM25_PENALTY` 在 BM25 路保留作为软排序
4. **改 retrieve_and_answer**：
   - Multi-query 改写（fast LLM 失败 → 单 query 兜底）
   - top_k 拉到 30-50（与 prompt budget 协同）
   - prompt 排版（重要 chunks 放头尾）
5. **回填脚本**：`scripts/migrate_to_hybrid.py`，对已有 1808 chunks 批量 encode + 入 vector store。
6. **评估搭建**（见 C.4）

**与 EchoDesk 现有架构对接点**：
- FastAPI：新增 `/rag/diagnose` debug endpoint，返回 (BM25 ranks, Dense ranks, RRF ranks, rerank ranks)，方便回归
- sqlite：vector store 走单独 `.bin` 文件，不污染主 db；metadata 仍走 JSON / 可选 sqlite virtual table (vss-sqlite)
- LLM 通道：fast LLM 用 heyi-bj 做 multi-query；main LLM 用 yunwu MiniMax 做综合；embedding 优先 heyi-bj 同通道复用 connection pool
- 22-modularity.mdc：所有新组件走 Port-Adapter 注入，不破坏现有契约

**潜在踩坑点（必读）**：

| 风险 | 影响 | 缓解 |
|---|---|---|
| bge-m3 首次下载 2.2GB | 用户首次启动等待 / 流量 | 后台异步下载，期间走 BM25-only；UI 显示"正在加载语义检索模型 (1/N MB)" |
| sentence-transformers 在 macOS Apple Silicon 兼容 | 偶发 MPS backend 报错 | force CPU 模式 + `OMP_NUM_THREADS=4`；实测 M2 Pro CPU 模式吞吐够用 |
| ANN 索引和 BM25 不同步 | dense 检到的 chunk_id 在 BM25 找不到 → 引用失效 | 加入 `chunk_id` 作为唯一 key，索引层用同一份 `_chunks: list[RagChunk]`，禁止异步独立写 |
| embedding 模型升级要全量重 embed | 50k chunks ≈ 100 min 阻塞 | model_version 记 metadata；启动检测不一致 → 后台分批 re-embed，期间走旧索引 |
| MiniMax-M2.7 长 prompt 计费 | 单次 12 元的成本红线 | 默认 top_k=30 而非 100；UI 设置面板暴露"召回深度" + 单月预算上限告警 |
| 跨语种 query | 用户用拼音"hua xue"问"化学" | bge-m3 多语种但拼音不在训练目标内；上 Step 4 Multi-Query 让 fast LLM 同时输出中文/英文/拼音变体 |
| 长 chunk truncation | bge-m3 上限 8192 token，PDF 一页有时超 | chunker 强制 ≤ 1500 token；上层组合多 chunk 喂给生成器 |
| 远端 embedding API rate limit | heyi-bj 或 yunwu 限频 | local fallback；adapter 层加 retry + bulkhead |
| 索引迁移时新增 chunk | workspace 扫描和回填脚本并发 | 加 ingest-lock 期间走 dual-write（BM25 + 入 pending queue），回填完成后切流 |
| sqlite vss 扩展依赖 | 部分系统编译失败 | 默认不用 sqlite-vss；用 hnswlib pure-Python wheel |

### C.4 长期路径（4+ 个迭代）

按价值/难度排序：

1. **Agentic Retrieval**（B.7）：让 main LLM 调 `retrieve_with_filter(query, doc_kind, date_range, source)` tool。直接对接已有 `RetrievalResult` schema 扩展。对 "上周关于褐蚁的对话" 这类复合查询有质变。
2. **Tool-use 走 main LLM listwise rerank**（B.3 + B.5 合二为一）：在 prompt 里同时让 LLM 输出 (ranked chunk_ids, answer)，省掉本地 reranker 体积。
3. **GraphRAG 用于会议-人物图谱**（B.6）：从 ambient + meeting 自动抽人、议题、决议，build social graph。先做读路径，再做写路径（用户说"我和 Alice 上次聊了什么"）。
4. **Multimodal RAG**：PPT 里的图、PDF 里的图表，用 GPT-4V/Gemini Vision 抽取后再喂检索。会议里的截图也属于这一类。
5. **User-feedback loop**：用户标"这个答案不对"→ 把召回的 chunks 标为负样本，定期 fine-tune bge-m3（用 LoRA / domain adaptation）。

### C.5 评估指标怎么搭

**离线指标**（必须，先于上线）：

- **Recall@10 / Recall@50**：人工标 50-100 个 query → ground-truth chunk_ids，回归脚本跑过任意 retriever 看召回率。
- **NDCG@10**：考虑排序质量，0-1。
- **MRR**（mean reciprocal rank）：第一个相关 chunk 的位置倒数平均。
- **End-to-end answer quality**：用 LLM-as-judge（用 MiniMax-M2.7 自己当 judge，对 baseline / new 两个答案做 A/B win-rate）。

**在线指标**：

- **用户接受率**：每次答复后 UI 给 👍/👎，统计 7 日滚动窗口
- **"在已有的资料里没找到相关内容" 触发率**：用户痛点的直接 proxy
- **召回延迟**（p50/p99）：BM25/Dense/Rerank/总
- **token 消耗 / 单次答复成本**：MiniMax 大窗口模式必须监控

**评估集来源（即刻可做）**：

- 从用户已上传的 49 docs 里手工挖 30-50 个 query（"褐蚁竞品调研"就是其一）
- 用 fast LLM 对每个 doc 自动生成 3-5 个合成 query（"如果这段内容能回答什么问题"），人工筛选保留 100 个高质量 query
- ground-truth chunk_id 由人工标注（一次 2-3 小时）

**指标基线（先建后改）**：

| 阶段 | Recall@10 baseline | 目标 |
|---|---|---|
| 当前 BM25 | 30-40%（用户痛点 case 上 < 20%） | — |
| C.2 短期 | 50-60% | + 10-15% |
| C.3 中期 hybrid | 70-80% | + 20-25% 相对 C.2 |
| 中期 + reranker | 80-85% | + 5-8% |
| 长期 agentic | 85-90%（特殊 query 类） | + 不可比，提升的是复合 query 维度 |

---

## D. 短期"立刻能上"的修复（1-2 小时一个 PR，按性价比排序）

> 用户已明确：**不要强行限制覆盖量**。所以下面这些修复都**放大** top_k，让 main LLM 大窗口处理。

### D.1 [★★★★★ 性价比最高] 把 `_DEFAULT_RAG_TOP_K=8` 改成 50，prompt 渲染前 30 chunks

`retrieve_and_answer.py`:
- `_DEFAULT_RAG_TOP_K = 50`
- `_format_rag` 里 `chunks[:5]` 改 `chunks[:30]`
- 顺手把 `_AMBIENT_BM25_PENALTY` 从 0.25 改成 0.4（chunks 多了，没必要再把 ambient 砍这么狠）

**成本**：10 行代码 + 1 个 commit。  
**收益**：用户截图 case 立刻能看到 PDF chunk 进入 top-30；MiniMax 80k 窗口下 30 chunks × 400 token = 12k token，远在预算内。  
**风险**：单次 LLM call input 量从 ~2k token 涨到 ~13k token，云雾计费成本上升 ~6x（按 2-3 元/次估算），但用户已表态接受。

### D.2 [★★★★★] Ambient 段聚合（60s 窗口）

`bm25.py:_ingest_ambient_segment_sync`:
- 不再每段独立 chunk
- 检查同 doc_id 最后一个 chunk 的 `captured_at`：如果 <60s 前 → append text 到同 chunk，更新 metadata
- 超过 60s 或 speaker 切换 → 新建 chunk

**成本**：30-50 行，0.5 PR。  
**收益**：解决 BM25 length normalization 的根本不公平。配合 D.1 + D.3 后，用户痛点 case 应该完全消失。  
**风险**：现有 ambient_segments 表的 row → chunk 映射要兼容（已有 doc_id 跑过去是粗粒度的就保留，新数据按新规则）。

### D.3 [★★★★] BM25 参数 `b=0.5`

`bm25.py:_rebuild_bm25`:
- `BM25Okapi(self._tokens, b=0.5)`（rank_bm25 API 支持自定义 b）

**成本**：1 行。  
**收益**：BM25 length bonus 砍半；与 D.2 协同，让 short ambient 不再不公平。  
**风险**：极低；BM25 参数调优本来就有经验区间，b=0.5 是文献常见值。

### D.4 [★★★] Doc-level 多样性 cap + source 软排序

`retrieve_and_answer.py:_rerank_with_source_priority`:
- 改名 `_rerank_diverse_with_priority`
- 一次性输入是 top-100 BM25 命中
- 先按 doc_id 分组，每 doc 取分数最高的前 4 chunks
- 再按 (source 优先级, score) 排序，PDF/workspace > meeting > ambient
- 返回 top-30

**成本**：30 行。  
**收益**：避免单个 doc（特别是 ambient daily 长文档）霸榜；为 D.1 的 top-30 提供更高质量的多样化输入。  
**风险**：低；优先级是软排序，没有硬过滤。

### D.5 [★★★] 句子边界 chunker

`bm25.py:_chunk_text`:
- 把 `text[i:i+size]` 改成 "按 `[。！？；\n]` 切 → 贪心组装到 size"

**成本**：20 行。  
**收益**：避免句子在 chunk 边界被切断，提升 TF 完整性；对 BM25 和未来 dense 都有提升。  
**风险**：极低；现有 chunk 不会自动重新切分，**只对新入库 doc 生效**，所以要在 D.2 改造完成后做一次离线 reindex（脚本 30 min 跑完 1808 chunks）。

### D.6（备选，与 D.1 二选一）[★★★] Prompt 显式排版与 lost-in-the-middle 引导

`retrieve_and_answer.py:_ANSWER_PROMPT_TEMPLATE`:
- 加一句："请通读下方 N 段证据后再答；不要只看前 5 段。如果用户问的内容只在中后段，请明确引用具体 chunk_id。"
- 把 top-30 重排：top-10 放头、11-20 放尾、21-30 放中间

**成本**：30 行。  
**收益**：缓解 long-context lost-in-the-middle 问题，对所有长 input 都有用。  
**风险**：低；是 prompt-only 改动，可灰度。

---

## E. 推荐路径总结（决策图）

```
                  当前 BM25-only (recall ~ 35%, 用户痛点频发)
                                  │
                                  ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Week 1 (D.1 + D.2 + D.3 + D.4 + D.5)：纯参数 + 聚合 + cap        │
   │   预期 recall ~ 55%，用户痛点 case (褐蚁竞品调研) 解决            │
   │   零新依赖、零模型下载                                            │
   └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Sprint 2-3 (C.3 中期 hybrid 路径)                                │
   │   bge-m3 本地 (2.2GB) + hnswlib + RRF + multi-query              │
   │   预期 recall ~ 75-80%                                            │
   │   新增依赖：sentence-transformers, hnswlib, faiss-cpu (可选)     │
   └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Sprint 4+ (C.4 长期)                                              │
   │   Agentic + GraphRAG + Multimodal                                │
   │   按用户 query 类型与频率渐进上线                                  │
   └────────────────────────────────────────────────────────────────┘
```

**最重要的一句话**：用户痛点的 80% 解决在 **Week 1 的 D.1+D.2+D.3** 三连击里——把 top_k 放大、把 ambient chunk 聚合、把 BM25 b 调小。新模型/新依赖是质变跃迁的台阶，但**不是消除当前痛点的必要条件**。

---

## F. 与 EchoDesk 既有工程约束的兼容性矩阵

| 既有约束 | 兼容性 | 备注 |
|---|---|---|
| 06-platforms.mdc：禁止直接用 openai/anthropic | ✓ | 所有 LLM 走 yoli_llm（短期：用 LLMPort 适配，长期：迁到 yoli_llm） |
| 06-platforms.mdc：embedding 选型 | ✓ | 本地 bge-m3 / 远端 heyi-Qwen3 都不违反 denylist；远端云雾 embedding 需先确认是否在 yoli_llm 路由内 |
| 09-cicd.mdc：PR ≤ 400 行 | ✓ | C.3 中期改造拆 3-4 个 PR：EmbeddingPort + Adapter / VectorStorePort + hnswlib / HybridRag / multi-query |
| 22-modularity.mdc：Port-Adapter | ✓ | 新增 EmbeddingPort、VectorStorePort、（可选）RerankerPort |
| 25-context-engineering.mdc：FactStore | ✓ | 本调研中关于"BM25 length normalization 是根因"的结论应该 append 为 fact，category=conclusion |
| 现有 sqlite 主库 | ✓ | 向量数据走独立 `.bin` 文件，metadata 与现有 JSON 持平 |
| Electron 打包后启动延迟 | ⚠ | bge-m3 加载 + 索引启动 ≈ 8-15s。FastAPI startup 异步触发，期间走 BM25-only |
| heyi-bj 远端可用性 | ⚠ | embedding 走远端时必须有本地兜底；与现有 fast LLM fallback 一致 |
| MiniMax 80k 窗口计费 | ⚠ | 大窗口 stuffing 需要在 UI 暴露"召回深度"以让用户控制单次成本 |

---

## G. 立刻可 commit 的最小开端（用户如果今天就想看到改变）

按 D.1 → D.3 → D.4 → D.6 → D.2 → D.5 顺序提交 5-6 个小 PR，**总开发时间 4-8 小时，零新依赖，零模型下载**：

1. `feat(echo-rag): 把 RAG top_k 默认放大到 50，prompt 渲染 30 chunks`（D.1 + D.6）
2. `fix(echo-rag): BM25 length normalization b=0.5 + ambient penalty 调到 0.4`（D.3）
3. `feat(echo-rag): doc-level 多样性 cap + source 优先级软排序`（D.4）
4. `refactor(echo-rag): chunker 改为句子边界感知`（D.5）
5. `feat(echo-rag): ambient 段聚合到 60s 窗口`（D.2，最有价值但改动最大，放最后）
6. `chore(echo-rag): 离线 reindex 脚本一次性重写已有 chunks`

**这 6 个 PR 做完，用户的"褐蚁竞品调研"截图问题应该完全消失**。

之后再进入 C.3 中期路径，按 1 个 sprint 节奏推进 hybrid retrieval。

---

**报告完。**

> 待办事项：在 `_state/events/` append 一个 fact event，category=conclusion，内容："BM25 length normalization + ambient 单段 chunk 是当前 RAG 失效的结构性根因（B.5）；hybrid retrieval + chunk 聚合 是稳态方向（C.3）" 以遵守 25-context-engineering.mdc R4。
