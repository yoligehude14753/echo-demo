# EchoDesk 使用埋点隐私合同

状态：本任务的本地 contract 设计；生产 ingestion、persistence、cross-user query 均未实现。
任务：`echo-core-privacy-telemetry`

## 1. 代码事实盘点

| 事实 | 证据 | 约束含义 |
|---|---|---|
| 当前仓库的身份对象包含 `tenant_id`、`owner_id`、`device_id`，这些字段是授权 scope，不是 telemetry label。 | `backend/app/security/models.py` | telemetry 只能在适配器边界把服务端输入转成 HMAC pseudonym，事件和聚合结果不得保存原始值。 |
| 当前产品指标文档明确不收集会议正文、文档内容、prompt、token 或 Artifact 文件，并要求未来行为埋点只记录动作类别、结果和耗时。 | `METRICS.md:8-9,74-75` | 本合同不允许 audio、transcript、summary、prompt、文件名或自由文本穿透。 |
| 当前数据模型迁移范围为 `001`–`039`，本任务不拥有新的 migration 编号。 | `docs/0.3/DATA_MODEL_AND_CONTRACTS.md:1-4` | 不创建 migration `040`，不创建 telemetry 表。 |
| 当前仓库没有已批准的生产 telemetry sink/外部查询入口；生产保留周期和外部指标存储仍待独立决策。 | `METRICS.md:134-136` | 本任务只交付独立 Port、默认 no-op 和测试用途 in-memory adapter；生产 ingestion/persistence 保持 blocked。 |
| telemetry 线不接入 Hub identity credential、Memory revision/CAS、同步 outbox，也不做 production wiring。 | Sol 并发所有权与阶段决策 | 新模块必须保持独立，避免与 v0.3.3 多端同步发生耦合。 |

## 2. 威胁模型

| 威胁 | 失败后果 | 结构性控制 |
|---|---|---|
| 调用点把正文或任意日志 body 传入 telemetry | 会议内容、转写或 prompt 被聚合/回传 | 事件模型 `extra=forbid`；字段全部是枚举、有限数值或受限 opaque token；没有 dict/body/text 字段。 |
| 原始 tenant/user/device id 进入事件、label、查询结果或日志 | 跨域身份暴露、可重建用户画像 | server-side HMAC；事件只保存 pseudonym、`key_version`、`epoch`；输入只在适配器内存中短暂存在。 |
| 同一身份跨 epoch 可被关联 | 轮换失效，形成长期轨迹 | HMAC 消息包含 subject kind 与 epoch；epoch 或 key version 变化时输出不同 pseudonym。 |
| 小 cohort 被查询反推 | 单用户行为被暴露 | query 对每个 cohort 执行可配置 `k` 抑制，低于阈值不返回 aggregate。 |
| failure reason 携带异常、response body 或 URL query | secret、Authorization/cookie、内部 URL 或正文泄露 | 只接受 `FailureReason` allowlist；拒绝自由文本。 |
| 重试/重复投递放大 request count | 成功率和用量失真 | `event_id` 是受限 opaque idempotency key；同 id 同 payload 只计一次，冲突 payload 拒绝。 |
| 默认配置误开启或 no-op 产生隐式写入 | 未同意的遥测收集 | feature flag 默认关闭；no-op 的所有写入口不修改状态；本任务不做 production wiring。 |
| retention/delete 不完整 | 用户撤回后仍保留聚合 | Port 提供 retention 与 delete hooks；in-memory adapter 只保留 pseudonymized events，并可重建聚合。 |
| telemetry 进入产品同步链路 | 计数/身份数据污染 Hub 双向同步 | 模块不导入 Hub、outbox、Memory 或 repository；独立内存适配器仅供测试。 |

## 3. 数据清单与边界

### 允许的最小数据

- 身份输入：服务端已验证的 tenant/user/device 标识只作为 HMAC 输入；不得作为事件字段、label、query filter 或日志值保存。
- 身份输出：`tenant_pseudonym`、`user_pseudonym`、`device_pseudonym`、`key_version`、`epoch`。
- 维度：`operation`、`platform`、合法化的 `app_version`、`provider`。
- 计数/时长：每事件固定计数 1；`success`；allowlisted `failure_reason`；end-to-end latency、queue wait、audio duration 的有限非负整数毫秒值。
- 幂等与生命周期：受限 `event_id`、UTC `occurred_at`；用于 retention、duplicate suppression 和 delete audit receipt，不作为用户可见数据。

### 明确禁止

`raw_audio`、audio bytes、transcript、summary、prompt、文件名、Artifact 内容、Authorization、cookie、API key、真实账号/邮箱/手机号、原始 tenant/user/device id、自由文本 error、response body、URL query、任意 dict、任意日志 body。

## 4. Typed event contract

`TelemetryEvent` 是唯一写入形状，未知字段拒绝。调用点只能构造以下 allowlist：

```text
event_id: opaque idempotency token
occurred_at: timezone-aware UTC datetime
identity: {tenant_id, user_id, device_id}  # 仅作为 adapter 的 HMAC 输入，不落事件
operation: allowlisted operation enum
platform: allowlisted platform enum
app_version: 合法化版本 token
provider: provider registry 中的内部枚举
success: bool
failure_reason: stable enum，成功时必须为空，失败时必须有值
end_to_end_latency_ms: bounded non-negative integer
queue_wait_ms: bounded non-negative integer
audio_duration_ms: bounded non-negative integer or null
```

适配器内部 materialize 后的事件只包含：

```text
event_id, occurred_at, pseudonymous identity, key_version, epoch,
operation, platform, app_version, provider, success, failure_reason,
end_to_end_latency_ms, queue_wait_ms, audio_duration_ms
```

旧客户端缺少可选的 `platform`、`provider`、`queue_wait_ms` 或 `audio_duration_ms` 时使用明确的 `unknown`/`0`/`null` 默认值；不猜测正文或 provider 自由文本。

## 5. Aggregate / query / retention contract

- 聚合 key：`epoch + key_version + tenant_pseudonym + user_pseudonym + device_pseudonym + operation + platform + app_version + provider`。
- 聚合值：`request_count`、`success_count`、`failure_count`、`success_rate`、typed `failure_reason_counts`、`latency_sum_ms`、`queue_wait_sum_ms`、`audio_duration_sum_ms`、`audio_duration_event_count`；failure reason 只能是 stable enum + count，不接受任意 map/text。
- `success_rate = success_count / request_count`；无事件不产生 cohort。
- query 只接受 typed `TelemetryQuery`（包括可选的 `FailureReason` enum filter）与 pseudonym filters，不接受 raw identity、自由文本、URL 或 request body；每个结果在返回前执行 `k_threshold` 抑制。
- retention 以 `occurred_at` 和配置的 retention window 删除事件；聚合从剩余事件重建，避免删除后残留计数。
- delete hook 按 pseudonymous identity 删除，返回不含身份的 `DeletionReceipt`，并记录删除数量/时间/原因枚举；生产身份到历史 epoch 的受控派生删除由未来独立 sink owner 负责。
- `NoopTelemetryAdapter` 的 `record`、`query`、`purge_expired`、`delete` 全部零写；无真实生产 sink。

## 6. 可证伪验收矩阵

| ID | 可证伪条件 | 证据 | 通过标准 |
|---|---|---|---|
| T-01 | 默认关闭的 runtime config 构造 no-op；record 前后 adapter 状态不变。 | 专用单测 | 0 stored events、0 aggregate、0 deletion audit write。 |
| T-02 | 同一 identity、同一 key version、同一 epoch 得到相同 pseudonym。 | pseudonym 单测 | 三类 subject 均稳定且 domain-separated。 |
| T-03 | key rotation 或 epoch 变化后 pseudonym 改变，旧值不能由输出字段直接关联。 | rotation/cross-period 单测 | pseudonym 不相等；输出仅含 pseudonym/version/epoch。 |
| T-04 | 任意 raw identity、credential、Authorization/cookie/API key、URL query 或自由文本输入不能成为事件字段/label/query 结果。 | negative scan + schema rejection 单测 | 事件构造拒绝；序列化结果不包含禁止 token；源码无禁止 telemetry payload 字段。 |
| T-05 | audio/transcript/summary/prompt/body 等字段不能穿透。 | negative scan + `extra=forbid` 单测 | 未知字段全部拒绝；无 `dict[str, Any]` telemetry write contract。 |
| T-06 | provider、platform、version、failure reason 只接受 registry/格式 allowlist。 | enum/version 单测 | 非法自由文本拒绝；成功事件不能携带 failure reason。 |
| T-07 | 同 event id 同 payload 重复提交。 | idempotency 单测 | request count 仍为 1；冲突 payload 明确拒绝。 |
| T-08 | 多事件按完整维度聚合成功/失败/reason/延迟/queue/audio。 | aggregate 单测 | 各计数、reason counts、sum、rate 精确匹配；不存 raw identity。 |
| T-09 | cohort 小于 k threshold 查询。 | suppression 单测 | 结果为空且不返回部分 aggregate。 |
| T-10 | 事件超过 retention window。 | retention 单测 | purge 后事件与重建 aggregate 均不存在。 |
| T-11 | delete hook 删除一个 pseudonymous identity。 | delete/audit 单测 | 仅目标事件删除；receipt 只含数量/时间/原因枚举，不含身份。 |
| T-12 | 旧客户端缺可选字段。 | compatibility 单测 | 使用显式 unknown/0/null 默认，仍能被 typed adapter 接收。 |
| T-13 | 独立模块没有 production wiring、Hub/outbox/repository/migration 依赖。 | import/source scan | telemetry 模块无这些依赖；migration 编号仍为 unassigned。 |

## 7. 冻结状态

```text
telemetry_contract=ready       # 仅在专用测试与独立复验通过后
local_in_memory_adapter=ready # 仅测试用途，不是生产持久能力
production_ingestion=blocked
production_persistence=blocked
cross_user_query=blocked
migration_number=unassigned
```

本文件不宣称可以读取其他用户数据，也不把本地 contract/in-memory 测试结果升级为 production ingestion/query 成功。
