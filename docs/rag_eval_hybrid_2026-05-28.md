# RAG 评估报告 · hybrid_2026-05-28

- **endpoint**: `http://localhost:8769/rag/ask`
- **rag_top_k**: 50
- **总 query 数**: 50
- **生成时间**: 2026-05-28T21:41:57

## 总分

| 指标 | 数值 |
|---|---|
| Recall@10 | 76.4% |
| Recall@30 | 78.4% |
| MRR | 0.5461 |
| 零召回 (recall@30=0) | 8 / 50 |

## 与 baseline 对比

| 指标 | baseline | 当前 | 差值 |
|---|---|---|---|
| Recall@10 | 72.5% | 76.4% | +3.9% |
| Recall@30 | 74.5% | 78.4% | +3.9% |
| MRR | 0.5394 | 0.5461 | +0.0067 |

## 按 category 分项

| category | count | Recall@10 | Recall@30 | MRR | 零召回数 |
|---|---|---|---|---|---|
| cross_doc | 10 | 91.7% | 91.7% | 0.4444 | 0 |
| summary | 10 | 100.0% | 100.0% | 0.3119 | 0 |
| technical_qa | 20 | 87.5% | 87.5% | 0.8850 | 1 |
| time_range | 10 | 15.5% | 25.5% | 0.2040 | 7 |

## 按 difficulty 分项

| difficulty | count | Recall@10 | Recall@30 | MRR | 零召回数 |
|---|---|---|---|---|---|
| easy | 20 | 86.7% | 91.7% | 0.6013 | 1 |
| medium | 20 | 79.6% | 79.6% | 0.5933 | 3 |
| hard | 10 | 49.7% | 49.7% | 0.3411 | 4 |

## 失败 query (Recall@30 = 0) · 共 8 条

### q010 · technical_qa · hard

- **query**: `褐蚁产品的价格`
- **expected_doc_ids**: `['pdf-bea09a0ef299', 'pdf-aa9c2de77e3e', 'pdf-ae42fdded1dd']`
- **got top10**: `[]`
- **notes**: 全线产品手册末尾或工作站手册末页一般含报价; 实际不一定齐, 标 hard.

### q041 · time_range · easy

- **query**: `今天的 ambient 录音内容`
- **expected_doc_ids**: `['ambient-20260528']`
- **got top10**: `[]`
- **notes**: 今天 = 2026-05-28, ambient-20260528 是唯一一份 ambient.

### q043 · time_range · hard

- **query**: `今天上午的会议有哪些`
- **expected_doc_ids**: `['meeting-m-bdd1da4e7e21', 'meeting-auto-1779882039', 'meeting-auto-1779938090', 'meeting-m-6af16d049596', 'meeting-m-7ffe56cc4ad8', 'meeting-auto-1779879582', 'meeting-auto-1779881070', 'meeting-auto-1779881361']`
- **got top10**: `[]`
- **notes**: 今天上午 = 2026-05-28 03:30-09:00 之间的会议; 标 top-8 个上午开始的.

### q045 · time_range · hard

- **query**: `今天聊得最多的话题`
- **expected_doc_ids**: `['meeting-m-4f1dedae3feb', 'meeting-auto-1779882039', 'meeting-auto-1779948835', 'meeting-auto-1779959260', 'meeting-auto-1779954231', 'ambient-20260528']`
- **got top10**: `[]`
- **notes**: 最大的几个会议 (chunk 数最多) + ambient.

### q046 · time_range · medium

- **query**: `今天的工作记录`
- **expected_doc_ids**: `['ambient-20260528', 'meeting-m-4f1dedae3feb', 'meeting-auto-1779948835', 'meeting-auto-1779953535']`
- **got top10**: `[]`
- **notes**: ambient + 几个有实质 minutes 的会议.

### q047 · time_range · medium

- **query**: `今天开会聊到了什么 AI 项目`
- **expected_doc_ids**: `['meeting-auto-1779953535', 'meeting-auto-1779954231', 'meeting-m-57d43dd37ad5', 'meeting-auto-1779955846']`
- **got top10**: `[]`
- **notes**: AI 会议助手 / 自动测评 / 河南职校 / 河南高校 4 个 AI 主题会议.

### q048 · time_range · hard

- **query**: `本周下午的会议有哪些`
- **expected_doc_ids**: `['meeting-auto-1779955846', 'meeting-auto-1779959260', 'meeting-m-57d43dd37ad5']`
- **got top10**: `['meeting-auto-1779953535', 'meeting-m-1df96a55f109', 'meeting-m-4f1dedae3feb', 'meeting-m-6af16d049596', 'meeting-auto-1779964288', 'meeting-auto-1779881361', 'meeting-auto-1779882039', 'meeting-auto-1779951656', 'meeting-auto-1779954231', 'meeting-auto-1779962034']`
- **notes**: 2026-05-28 下午时段 (>=12:00) 的会议; 08:00-10:30 UTC ≈ 16:00-18:30 北京时间. (auto-1779962034 在 meetings/ 但 /rag/docs 已过滤, 不计入 expected.)

### q049 · time_range · medium

- **query**: `今天 ambient 录音里关于褐蚁的片段`
- **expected_doc_ids**: `['ambient-20260528']`
- **got top10**: `[]`
- **notes**: ambient-20260528 是唯一一份 ambient doc.

## 全部 query 明细 (按 id 排序)

| id | category | difficulty | Recall@10 | Recall@30 | first_hit_rank |
|---|---|---|---|---|---|
| q001 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q002 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q003 | technical_qa | easy | 100.0% | 100.0% | 2 |
| q004 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q005 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q006 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q007 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q008 | technical_qa | medium | 66.7% | 66.7% | 1 |
| q009 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q010 | technical_qa | hard | 0.0% | 0.0% | — |
| q011 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q012 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q013 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q014 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q015 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q016 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q017 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q018 | technical_qa | easy | 100.0% | 100.0% | 5 |
| q019 | technical_qa | medium | 75.0% | 75.0% | 1 |
| q020 | technical_qa | medium | 75.0% | 75.0% | 1 |
| q021 | summary | easy | 100.0% | 100.0% | 2 |
| q022 | summary | easy | 100.0% | 100.0% | 7 |
| q023 | summary | easy | 100.0% | 100.0% | 7 |
| q024 | summary | easy | 100.0% | 100.0% | 1 |
| q025 | summary | easy | 100.0% | 100.0% | 6 |
| q026 | summary | easy | 100.0% | 100.0% | 6 |
| q027 | summary | medium | 100.0% | 100.0% | 6 |
| q028 | summary | medium | 100.0% | 100.0% | 2 |
| q029 | summary | medium | 100.0% | 100.0% | 6 |
| q030 | summary | easy | 100.0% | 100.0% | 6 |
| q031 | cross_doc | hard | 100.0% | 100.0% | 1 |
| q032 | cross_doc | medium | 100.0% | 100.0% | 2 |
| q033 | cross_doc | medium | 100.0% | 100.0% | 1 |
| q034 | cross_doc | medium | 100.0% | 100.0% | 6 |
| q035 | cross_doc | medium | 100.0% | 100.0% | 5 |
| q036 | cross_doc | medium | 100.0% | 100.0% | 6 |
| q037 | cross_doc | hard | 100.0% | 100.0% | 8 |
| q038 | cross_doc | hard | 66.7% | 66.7% | 1 |
| q039 | cross_doc | hard | 50.0% | 50.0% | 7 |
| q040 | cross_doc | hard | 100.0% | 100.0% | 7 |
| q041 | time_range | easy | 0.0% | 0.0% | — |
| q042 | time_range | easy | 0.0% | 100.0% | 25 |
| q043 | time_range | hard | 0.0% | 0.0% | — |
| q044 | time_range | hard | 80.0% | 80.0% | 1 |
| q045 | time_range | hard | 0.0% | 0.0% | — |
| q046 | time_range | medium | 0.0% | 0.0% | — |
| q047 | time_range | medium | 0.0% | 0.0% | — |
| q048 | time_range | hard | 0.0% | 0.0% | — |
| q049 | time_range | medium | 0.0% | 0.0% | — |
| q050 | time_range | medium | 75.0% | 75.0% | 1 |
