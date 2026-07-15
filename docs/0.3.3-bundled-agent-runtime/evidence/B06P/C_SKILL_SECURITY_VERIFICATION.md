# B06P-C — skill-and-security-verification evidence

状态：`ACCEPTED_CANDIDATE`

本证据属于 B06P subagent C，base 为 `e7bacd136f77cfeba157af0dc6151b692b43ac05`；主任务尚未汇总提交，故本文件不声明最终 head 或独立 commit。

## 写集与 delta probe

- 写集：`backend/app/agent_capabilities/skill_host.py`、对应 table-driven harness，以及本证据文件。
- 未修改 B03 `agent_capabilities` 事实源、A/B host、公共 invocation/registry/glue。
- 必要 delta probe：读取 B03 `GrantSnapshot`、`SKILL_USE`、`evaluate_capability`、stable deny code 与 redaction contract；确认 B03 的 `InvocationBinding` 没有 `toolUseId` 字段。C 在 host-owned envelope 中显式绑定 `taskId/operationKey/toolUseId/grantRevision`，再把 authority decision 委托给 B03；没有改写 B03。
- 继承的 F04 fake tool/cancel/mismatch trace 未重放。

## 实现证明

- `SkillManifest`：canonical signed payload、manifest SHA-256、identity/version、required capabilities、platform、resource SHA-256、signer 与 safe provenance。
- `SkillResolver`：只接受显式 bundle root；逐资源检查路径 containment、symlink、存在性、内容 hash、平台与签名；不读取 HOME/PATH，不读取全局 config。
- `EchoSkillHost`：只执行已注册的 embedded handler，不执行 manifest 提供的脚本；执行前后重新检查当前 GrantSnapshot、取消和 revision；额外能力声明默认 `SKILL_CAPABILITY_DEFERRED`，保持 fail closed。
- `SkillReceipt`：绑定 task/operation/toolUse/grant/policy revision，记录 skill identity/version、manifest/resource hash、signer/provenance 和 input/output hash；不记录 payload、output、异常文本、路径、URL、secret。
- P0：资源中检测到 Claude、`.claude`、hooks、npm/pnpm/yarn/pip install、HOME/PATH 或环境读取时返回 `UNSUPPORTED_P0_FAIL_CLOSED`。

## Focused verification

1. touched compile/typecheck 组：

   `ruff check backend/app/agent_capabilities/skill_host.py backend/tests/unit/agent_capabilities/test_skill_host.py && python3 -m py_compile ...`

   结果：`All checks passed`。

2. table-driven capability/security suite：

   `PYTHONPATH=backend pytest -q backend/tests/unit/agent_capabilities/test_skill_host.py`

   结果：`17 passed in 0.11s`。

   覆盖：macOS/Windows/Linux、signed manifest、资源 hash mismatch、provenance、skill allow/deny、task/operation mismatch、toolUseId 缺失、grant revision mismatch、revoke、cancel、额外 capability 默认拒绝、handler failure、无 raw secret receipt。

3. unsupported/redaction/runtime fallback scan：

   `ruff check ... && python3 -m py_compile ... && python3 <AST scan>`

   结果：`All checks passed`；`unsupported runtime fallback AST scan: PASS`；forbidden imports/calls none；explicit bundle root + injected verifier/handler PASS。

## Cleanup / redaction / provenance

- 本 host 不创建子进程，因此没有 task-owned process cleanup surface；handler 失败、取消和撤销均不返回 handler output，并生成 receipt。
- receipt 的输入/输出只保存 SHA-256；测试确认 `do-not-log` 与 `secret-token-value` 不出现在 receipt JSON，且 `redacted=true`。
- manifest hash 从去除 signature 的 canonical payload 计算；资源 hash 从真实临时 bundle 文件读回计算；签名 verifier 只使用显式注入的 signer key，不从环境或 HOME 发现密钥。

## 未执行项

- 未执行 F04 replay、全量回归、安装态矩阵、正式 package、runtime npm install、PATH/HOME fallback、Claude hooks。

## C verdict

`ACCEPTED_CANDIDATE`：C 写集 focused evidence 已闭合；最终 B06P verdict 仍由主任务在汇总 A/B evidence、公共 glue、scope audit 和 clean worktree 后裁定。
