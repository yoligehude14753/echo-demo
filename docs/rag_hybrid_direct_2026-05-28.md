# RAG 评估报告 · hybrid_direct_2026-05-28

- **endpoint**: `direct://HybridRag.query`
- **rag_top_k**: 50
- **总 query 数**: 50
- **生成时间**: 2026-05-28T21:02:34

## 总分

| 指标 | 数值 |
|---|---|
| Recall@10 | 83.8% |
| Recall@30 | 83.8% |
| MRR | 0.7589 |
| 零召回 (recall@30=0) | 2 / 50 |

## 与 baseline 对比

| 指标 | baseline | 当前 | 差值 |
|---|---|---|---|
| Recall@10 | 72.5% | 83.8% | +11.3% |
| Recall@30 | 74.5% | 83.8% | +9.3% |
| MRR | 0.5394 | 0.7589 | +0.2195 |

## 按 category 分项

| category | count | Recall@10 | Recall@30 | MRR | 零召回数 |
|---|---|---|---|---|---|
| cross_doc | 10 | 91.7% | 91.7% | 0.7533 | 0 |
| summary | 10 | 100.0% | 100.0% | 0.7333 | 0 |
| technical_qa | 20 | 80.0% | 80.0% | 0.9000 | 1 |
| time_range | 10 | 67.2% | 67.2% | 0.5076 | 1 |

## 按 difficulty 分项

| difficulty | count | Recall@10 | Recall@30 | MRR | 零召回数 |
|---|---|---|---|---|---|
| easy | 20 | 85.8% | 85.8% | 0.7467 | 1 |
| medium | 20 | 90.8% | 90.8% | 0.7738 | 0 |
| hard | 10 | 65.5% | 65.5% | 0.7533 | 1 |

## 失败 query (Recall@30 = 0) · 共 2 条

### q018 · technical_qa · easy

- **query**: `HY90 硬件清单`
- **expected_doc_ids**: `['csv-301bcacbee44']`
- **got top10**: `['pdf-aa9c2de77e3e', 'pdf-eff3f1d7b3ee', 'ambient-20260528', 'pdf-ae42fdded1dd', 'meeting-m-4f1dedae3feb', 'pdf-bea09a0ef299', 'meeting-auto-1779954231', 'pdf-be59a1550076']`
- **notes**: csv 标题就是 'HY90 BOM'.

### q048 · time_range · hard

- **query**: `本周下午的会议有哪些`
- **expected_doc_ids**: `['meeting-auto-1779955846', 'meeting-auto-1779959260', 'meeting-m-57d43dd37ad5']`
- **got top10**: `['ambient-20260528', 'meeting-auto-1779953535', 'meeting-m-1df96a55f109', 'meeting-m-4f1dedae3feb', 'meeting-auto-1779954231', 'meeting-m-7ffe56cc4ad8', 'meeting-auto-1779948835']`
- **notes**: 2026-05-28 下午时段 (>=12:00) 的会议; 08:00-10:30 UTC ≈ 16:00-18:30 北京时间. (auto-1779962034 在 meetings/ 但 /rag/docs 已过滤, 不计入 expected.)

## 全部 query 明细 (按 id 排序)

| id | category | difficulty | Recall@10 | Recall@30 | first_hit_rank |
|---|---|---|---|---|---|
| q001 | technical_qa | medium | 100.0% | 100.0% | 2 |
| q002 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q003 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q004 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q005 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q006 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q007 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q008 | technical_qa | medium | 66.7% | 66.7% | 1 |
| q009 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q010 | technical_qa | hard | 66.7% | 66.7% | 1 |
| q011 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q012 | technical_qa | easy | 66.7% | 66.7% | 1 |
| q013 | technical_qa | medium | 100.0% | 100.0% | 1 |
| q014 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q015 | technical_qa | easy | 100.0% | 100.0% | 2 |
| q016 | technical_qa | easy | 50.0% | 50.0% | 1 |
| q017 | technical_qa | easy | 100.0% | 100.0% | 1 |
| q018 | technical_qa | easy | 0.0% | 0.0% | — |
| q019 | technical_qa | medium | 75.0% | 75.0% | 1 |
| q020 | technical_qa | medium | 75.0% | 75.0% | 1 |
| q021 | summary | easy | 100.0% | 100.0% | 3 |
| q022 | summary | easy | 100.0% | 100.0% | 1 |
| q023 | summary | easy | 100.0% | 100.0% | 2 |
| q024 | summary | easy | 100.0% | 100.0% | 2 |
| q025 | summary | easy | 100.0% | 100.0% | 2 |
| q026 | summary | easy | 100.0% | 100.0% | 1 |
| q027 | summary | medium | 100.0% | 100.0% | 2 |
| q028 | summary | medium | 100.0% | 100.0% | 1 |
| q029 | summary | medium | 100.0% | 100.0% | 1 |
| q030 | summary | easy | 100.0% | 100.0% | 1 |
| q031 | cross_doc | hard | 100.0% | 100.0% | 1 |
| q032 | cross_doc | medium | 100.0% | 100.0% | 3 |
| q033 | cross_doc | medium | 100.0% | 100.0% | 1 |
| q034 | cross_doc | medium | 100.0% | 100.0% | 2 |
| q035 | cross_doc | medium | 100.0% | 100.0% | 2 |
| q036 | cross_doc | medium | 100.0% | 100.0% | 1 |
| q037 | cross_doc | hard | 100.0% | 100.0% | 5 |
| q038 | cross_doc | hard | 66.7% | 66.7% | 1 |
| q039 | cross_doc | hard | 50.0% | 50.0% | 1 |
| q040 | cross_doc | hard | 100.0% | 100.0% | 1 |
| q041 | time_range | easy | 100.0% | 100.0% | 2 |
| q042 | time_range | easy | 100.0% | 100.0% | 10 |
| q043 | time_range | hard | 25.0% | 25.0% | 3 |
| q044 | time_range | hard | 80.0% | 80.0% | 1 |
| q045 | time_range | hard | 66.7% | 66.7% | 1 |
| q046 | time_range | medium | 50.0% | 50.0% | 1 |
| q047 | time_range | medium | 75.0% | 75.0% | 7 |
| q048 | time_range | hard | 0.0% | 0.0% | — |
| q049 | time_range | medium | 100.0% | 100.0% | 2 |
| q050 | time_range | medium | 75.0% | 75.0% | 2 |
