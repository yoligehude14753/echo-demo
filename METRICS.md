# EchoDesk v0.3.1 指标体系

状态：指标定义完成；生产基线待发布后采集
日期：2026-07-12

## 1. 测量原则

1. 发布门禁、生产指标和外部 provider 可用性分别统计，不能互相替代。
2. 指标按 tenant / owner 聚合结果，不收集或导出会议正文、文档内容、prompt、token 或 Artifact 文件。
3. retry 以 logical objective lineage 聚合，避免一次失败后成功被计算成两个独立用户任务。
4. 用户主动取消不计入成功率分母，但单独统计取消收敛率。
5. 生产基线不存在时明确写“未知”，不得用 `1 / 1` 安装态测试冒充真实用户基线。

## 2. 北极星指标

### 已授权 Workflow 有效闭环率

定义：统计周期内进入非主动取消终态的 logical workflow objective 中，最后一次有效 attempt 为 `succeeded` 的比例。

```text
有效闭环率 = succeeded objectives
             / (succeeded + failed + timeout + cancel_failed objectives)
```

口径：

- objective 由 `parent_run_id` / retry lineage 递归归并到 root run。
- 同一 objective 只取最后一个已确认 attempt。
- `cancelled` 作为用户主动终止单独统计，不进入分母。
- 尚未终态的 `pending`、`running`、`cancel_requested` 不提前计为成功或失败；超过 deadline 后必须先由系统收口。
- 按 `kind`、运行模式、tenant 和 owner 聚合；不跨 tenant 合并资源明细。

当前生产基线：**未知，尚未采集**。
current exact-SHA release-gate 记录：真实安装态完整 workflow `1 / 1 passed`。该结果只证明受控验收路径，不代表生产基线。
v0.3.1 发布后 30 天目标：`>= 95%`。
频率：weekly；事故期间 daily。

## 3. 输入指标

| 指标 | 计算口径 | 当前数据源 | 状态 |
|---|---|---|---|
| Meeting finalize 成功率 | `meeting.finalize` objective 最终 succeeded / 有效终态 | `workflow_runs`、`workflow_events` | 可计算 |
| Artifact 生成成功率 | `artifact.generate` objective 最终 succeeded / 有效终态 | `workflow_runs`、`artifacts`、`artifact_links` | 可计算 |
| Retry 恢复率 | 首次 failed/timeout 且后续 attempt succeeded 的 objective / 发生 retry 的 objective | run lineage | 可计算 |
| Agent 任务成功率 | Agent objective 最终 succeeded / 有效终态 | `agent_tasks`、`workflow_runs` | 可计算 |
| Agent bridge 恢复率 | lease/heartbeat recovery 后最终收口的 task / 进入 recovery 的 task | task events、lease/recovery logs | 需要离线聚合 |
| RAG 写入成功率 | ingest/delete/scan/projection succeeded / 有效终态 | workflow + RAG manifest | 可计算 |
| RAG 有引用回答率 | 带 citations 且收到 `done` 的 answer / 收到 `done` 的 answer | 当前仅请求级可见 | 待匿名聚合埋点 |
| 会议到首个产物转化率 | 结束会议后产生 linked Artifact 的 meeting / ended meeting | meetings、artifact_links | 可计算 |
| Workflow 恢复时长 | 进程恢复开始到 run 再次运行或终态的时间 | workflow events | 需要离线聚合 |

未标目标的输入指标先建立两周基线，再由真实分布设目标，避免用测试样本虚构生产阈值。

## 4. 可靠性与一致性护栏

| 护栏 | 目标 | 检测方式 |
|---|---:|---|
| public 跨 principal 读成功 | `0` | isolation smoke + HTTP/WS negative tests |
| public 跨 principal 写成功 | `0` | isolation smoke + repository tests |
| permanent Agent/Workflow terminal divergence | `0` | terminal race tests + recovery audit |
| domain commit 成功但 run/event 缺失 | `0` | Unit of Work counterexamples |
| 已提交 outbox 永久未投影 | `0` | pending age、consumer recovery state |
| 同 scope 双 active meeting | `0` | unique index + migration audit |
| 用户清除纪要后被恢复重建 | `0` | tombstone regression |
| public 未登记 Artifact 文件下载成功 | `0` | download authorization tests |
| deterministic suite skipped | `0` | JUnit parse gate |
| secret 出现在 diagnostics/log | `0` | redaction tests + diagnostics inspection |

## 5. 身份与配额指标

以下指标只记录计数和稳定错误类别，不记录 credential/token：

- enroll、renew、rotate、additional-device、revoke 的成功/401/409/429 计数；
- active session family 和已撤销 family 数；
- admission 拒绝数与原因类别；
- quota accepted/rejected 单位和 ledger reconciliation 差异；
- 401 后仍继续使用旧 owner 的客户端行为数，目标为 `0`；
- 426 / 4426 触发数、最低版本和客户端停止重试数；目标是旧客户端不产生后续 session / business / WS 尝试；
- Electron `backend_origin` mismatch 拒绝数；目标是跨 origin bearer 发送数恒为 `0`；
- 设备身份恢复/轮换失败率。

当前 SQLite 已有 identity、admission、session 与 quota ledger；跨版本趋势面板尚未实现，不能把表存在写成生产 dashboard 已上线。

## 6. UX 指标

当前可由自动化稳定测量：

- E2E/scenario 通过率；
- 411 / 960 / 1280 / 1920 viewport 的横向溢出检查；
- 关键 dialog、drawer、button 的 accessible name；
- onboarding replay、focus restoration、error/retry 路径。

生产端尚未实现行为分析埋点。若未来增加，只允许记录动作类别、结果和耗时，不记录 transcript、prompt、文件名或会议标题；必须提供关闭选项。

## 7. 发布门禁指标

当前 v0.3.1 本地源码与受控安装态快照 [F-ECHO-028]（跨平台 hosted runner 结果另列在最终交接）：

| 门禁 | 结果 |
|---|---|
| Backend deterministic | `1045 collected`；`18 live deselected`；`1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`；line coverage `87.46%`（终端显示 `87%`）；进程自然退出 |
| Backend static | Ruff check 通过；Ruff format `250 files`；mypy `128 source files`；compile 通过 |
| Electron contracts | `177 / 177 passed` |
| Desktop E2E | `150 passed` |
| Desktop scenarios | `29 passed` |
| Public isolation | self-test 与双 principal 完整 smoke 通过 |
| Release aggregate | `31 / 31 passed`；actionlint 与 action pins 通过 |
| Android / TV current exact-SHA | phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)`、unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，Capacitor `Hint 2` 单列；debug APK 不可发布 |
| Dependency audit | npm 两处 `0`；Python six locks 均有效，runtime/dev/build 各有同一项上游无 `fix_versions` 的受控 `torch` `CVE-2025-3000`，例外至 2026-08-12；lint/typecheck/audit-tool 为 `0` |
| current exact-SHA macOS package | fresh ad-hoc arm64 DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066`、SHA-256 通过；read-only DMG smoke `1 / 1 passed` |
| current exact-SHA installed / live | 完整 workflow `1 / 1 passed`，覆盖真实下载 `0600`、marker、安全文件名、无 partial、GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart；live `2 / 2 passed`、`0 skipped / 0 failed` |
| 正式 Apple 签名链 | Developer ID、notary、staple、Gatekeeper：external skipped；ad-hoc 结果不可替代 |

这些结果是 release gate，不进入北极星生产分母。

截至 2026-07-13，公共状态仍为 GitHub Release `v0.2.50`、生产 backend `0.2.49`、bootstrap `app_version=0.2.45` 且没有 `minimum_client_version` [F-ECHO-029]。正式 signed cross-platform 候选、受保护 environment/secret 与 public cutover 仍是外部阻塞；不能把本地、开发签名或 unsigned 结果计为正式发布通过。

## 8. 已知 P2 的专门监控

### SQLite 长期容量

RAG blob、ambient WAV 与 transcript inject 已计量；meeting、workflow、event 等长期元数据仍依赖运维 retention。后续需要建立按 principal 的保留策略、水位、增长率和备份容量告警，并把清理结果纳入审计记录。

## 9. 数据来源与保留

| 来源 | 用途 | 内容边界 |
|---|---|---|
| SQLite workflow/domain tables | 成功率、lineage、恢复与隔离 | 仅聚合状态和时间，不导出正文 |
| structured logs | 错误分类、lease/outbox recovery | 先经过 redaction |
| CI JUnit / coverage | 发布门禁 | 作为 CI artifact，不提交到源码仓库 |
| public isolation smoke | 跨主体负例 | 随机 smoke id，不使用真实用户内容 |
| 客户端自动化 | UX 与 contract | mock/隔离运行目录 |

生产数据保留周期和外部指标存储尚未确定；在形成单独隐私与运维决策前，不新增远程内容 telemetry。

## 10. 复核节奏

- 每次 PR：确定性测试、contract、供应链和版本门禁。
- 每次 release：安装态 workflow、packaged smoke、签名、SBOM、hash 与 rollback。
- 每周：北极星、失败分类、retry 恢复、outbox/lease backlog、身份与 quota 异常。
- 每次事故：保留 run/event lineage 和经过脱敏的诊断证据，结论进入 FactStore event，而不是覆盖历史事实。
