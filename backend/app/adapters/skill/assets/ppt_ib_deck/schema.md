# ib_master_v3.pptx — Mustache 字段 Schema (v3)

> 总字段数 **27**（硬上限 30）。所有字段必填——缺字段或多字段都会让 docxtemplater 渲染失败。
> Mustache delimiters: `{` / `}`（不是双花括号，与 `render.mjs` 配置一致）。
> 输出 PPTX 14 页：封面 / 免责声明 / 目录 / 3 个章节扉页 / 7 个内容页 / 闭幕页。

## 全局约束

1. **数字必须来自 brief**（130.5 / 88% / 75% / $3500 亿 / $850 / +25%）。禁止编造数据。
2. **专有名词保英文**（Blackwell / B100 / B200 / GB200 NVL72 / Hopper / CUDA / NeMo / Triton / NIM / Microsoft / Meta / Google / Amazon / OpenAI / xAI / Anthropic / AMD / TPU / Trainium / AWS / PJM / ERCOT / BIS / Bloomberg / Goldman Sachs / NVDA），其它叙述使用简体中文。
3. **JSON 出口**：response 内必须**只有一个** ```` ```json ```` 代码块，前后不得有寒暄、解释、Markdown 注释。
4. **不要 nested**：所有字段都是 flat scalar（string），不能用数组 / 对象 / 嵌套。
5. **不要出现裸 `{` 或 `}`**（除了 JSON 本身的语法字符），避免触发 Mustache 误解析；如需在文本中表达数学集合等，用全角 `｛｝` 或 `[` `]`。

---

## 字段总览

| # | 字段 | 类型 | Max chars | 出现页 | 语义 |
|---|---|---|---|---|---|
| 1 | `cover_title` | string | 28 | 1 封面 | 报告主标题（54pt Serif），如 "英伟达 FY2026-FY2027 投资展望" |
| 2 | `cover_subtitle` | string | 80 | 1 封面 | 副标题（18pt Sans 钢灰），评级 + TP + 一句话 thesis |
| 3 | `disclaimer_body` | string | 600 | 2 免责声明 | 2-4 段免责声明正文，用 `\n\n` 分段，11.5pt |
| 4 | `es_b1` | string | 90 | 5 执行摘要 | 第 01 条核心观点，含至少 1 个 brief 数字 |
| 5 | `es_b2` | string | 90 | 5 执行摘要 | 第 02 条核心观点 |
| 6 | `es_b3` | string | 90 | 5 执行摘要 | 第 03 条核心观点（含评级 / TP / 催化） |
| 7 | `kpi1_value` | string | 12 | 6 KPI 快照 | KPI 1 数值（label 已预制为 "TOTAL REVENUE FY2025"），如 `$130.5B` |
| 8 | `kpi2_value` | string | 12 | 6 KPI 快照 | KPI 2 数值（label "DATA CENTER MIX"），如 `88%` |
| 9 | `kpi3_value` | string | 12 | 6 KPI 快照 | KPI 3 数值（label "DC GROSS MARGIN"），如 `~75%` |
| 10 | `kpi4_value` | string | 12 | 6 KPI 快照 | KPI 4 数值（label "HYPERSCALER CAPEX 2025E"），如 `$3,500B` |
| 11 | `th_lead` | string | 70 | 8 投资逻辑 | 一句金句（金色斜体 Serif），点出整章 thesis |
| 12 | `th_b1` | string | 110 | 8 投资逻辑 | PILLAR 01 卡片正文 |
| 13 | `th_b2` | string | 110 | 8 投资逻辑 | PILLAR 02 卡片正文 |
| 14 | `th_b3` | string | 110 | 8 投资逻辑 | PILLAR 03 卡片正文 |
| 15 | `mk_lead` | string | 70 | 9 市场格局 | 一句金句，AI capex 整体定调 |
| 16 | `mk_b1` | string | 110 | 9 市场格局 | 市场要点 1（含数字） |
| 17 | `mk_b2` | string | 110 | 9 市场格局 | 市场要点 2（含数字） |
| 18 | `cp_r1` | string | 95 | 10 竞争表格 | 第 1 行（AMD MI300X/MI350）威胁评估 |
| 19 | `cp_r2` | string | 95 | 10 竞争表格 | 第 2 行（Google TPU + AWS Trainium2）威胁评估 |
| 20 | `cp_r3` | string | 95 | 10 竞争表格 | 第 3 行（华为昇腾 / 寒武纪 / 摩尔线程）威胁评估 |
| 21 | `rk_b1` | string | 110 | 12 风险 | R-01 REGULATORY 风险（含中国出口管制 / 营收占比） |
| 22 | `rk_b2` | string | 110 | 12 风险 | R-02 COMPETITIVE 风险（AMD / TPU） |
| 23 | `rk_b3` | string | 110 | 12 风险 | R-03 VALUATION 风险（含估值水平 / 回撤幅度） |
| 24 | `rec_action` | string | 4 | 13 推荐 | 评级单词，必须是 `BUY`（大写） |
| 25 | `rec_target` | string | 8 | 13 推荐 | 目标价，必须是 `$850` |
| 26 | `rec_upside` | string | 8 | 13 推荐 | 隐含 upside，必须是 `+25%` |
| 27 | `closing_tagline` | string | 60 | 14 闭幕 | 闭幕一句话（金色斜体 Serif 28pt），如 "Thank You · Q & A" |

---

## 字段细节与示例

### `cover_title`

封面 60pt Serif 主标题。**不超过 28 个字符**（一行能放下）。

> 推荐写法：`英伟达 FY2026-FY2027 投资展望`

### `cover_subtitle`

封面副标题。一行 + 80 字以内。

> 推荐写法：`BUY · 12mo TP $850 · 数据中心营收占比 88% · 全球 AI capex 万亿级蓝海`

### `disclaimer_body`

2-4 段免责声明正文。用 `\n\n` 分隔段落。**不要**使用 markdown 加粗 / bullet。

### `es_b1` / `es_b2` / `es_b3`

3 条执行摘要，每条 90 字以内。

- `es_b1`：FY25 营收 / 5 年 CAGR / 数据中心占比（必须包含数字）
- `es_b2`：Blackwell 出货 / 产品代差
- `es_b3`：评级 + TP + Upside + 三大催化

### `kpi1..4_value`

四个大数字。Label 已固定在模板中，**只填数值字符串**：

| 字段 | label (在模板中) | 期望数值 |
|---|---|---|
| `kpi1_value` | `TOTAL REVENUE FY2025` | `$130.5B` |
| `kpi2_value` | `DATA CENTER MIX` | `88%` |
| `kpi3_value` | `DC GROSS MARGIN` | `~75%` |
| `kpi4_value` | `HYPERSCALER CAPEX 2025E` | `$3,500B`（或 `~$3.5T`） |

数值用 ASCII 数字 + 符号，**不要中文单位**（不要"亿"），让等宽数字字体生效。

### `th_lead` / `mk_lead`

一句金句（金色斜体 Serif，背景深海军蓝色块）。**不要再次罗列数字**，写整章定调。

### `th_b1..3`、`mk_b1..2`

正文要点。控制在 110 字以内（卡片高度有限）。

### `cp_r1..3`

竞争对手表格"Threat Assessment"列。

| 行 | 已固定的 Competitor 列 |
|---|---|
| `cp_r1` | `AMD Instinct · MI300X / MI350` |
| `cp_r2` | `Google TPU v5/v6 · AWS Trainium2` |
| `cp_r3` | `华为昇腾 910C · 寒武纪 · 摩尔线程` |

每条威胁评估 95 字以内。

### `rk_b1..3`

| 字段 | category | 必须包含 |
|---|---|---|
| `rk_b1` | REGULATORY | BIS 出口管制 + 中国营收占比 (26% → 13%) |
| `rk_b2` | COMPETITIVE | AMD MI350 / Google TPU v6 / 软件生态差距 |
| `rk_b3` | VALUATION | 前瞻 P/E + 潜在回撤幅度 |

### `rec_action` / `rec_target` / `rec_upside`

硬编码值：

```json
"rec_action": "BUY",
"rec_target": "$850",
"rec_upside": "+25%"
```

### `closing_tagline`

闭幕页（纯深海军蓝底 + 中心金色 GS emblem）下方的一句话，60 字内。

> 推荐写法：`Thank You · Q & A · Goldman Sachs Equity Research`

---

## 校验清单（render 前自检）

- [ ] JSON 严格语法（无尾逗号、键全双引号）
- [ ] 27 个字段全部存在
- [ ] 无多余字段
- [ ] 关键数字至少出现：`130.5`、`88%`、`75%`、`850`、`+25%`、`26%`、`13%`、`Blackwell`、`CUDA`
- [ ] 无 nested 结构
- [ ] response 只有一个 `json` 代码块
