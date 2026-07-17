# B06P-A file-mutation-hosts evidence

状态：`BLOCKED_CONTRACT_CHANGE_REQUESTED`（A 写集 focused evidence）

## 基线与 delta probe

- base：`e7bacd136f77cfeba157af0dc6151b692b43ac05`，即总控指定的 B03 accepted foundation。
- B03 已提供 immutable `GrantSnapshot`、capability catalog、pure `evaluate_capability`、stable deny code 和纯脱敏函数；因此本写集的必要 production delta probe 为源码确认，不重放 F04 fake tool/cancel/mismatch。
- B03 的 public snapshot 没有 `toolUseId` 或撤销字段。A 在自有 host context 中增加强制 `ToolInvocation` 绑定，并通过 `current_grant` 在执行前与 commit 前 recheck；缺少 current snapshot、revision 不匹配、cancel 或 revoke 均 fail closed。
- B03 compiler 生成的 workspace identity 是固定的 `host-verification-required` placeholder。A 不猜测或覆盖该事实；`PathVerifier` 只有在真实 root `dev:ino` identity 与 grant identity 相等时才允许 host policy 通过，否则返回 identity/ambiguity deny。因此当前 B03 compiler → file host 的真实 allow 路径被阻塞；需要 B03 提供 host-bound identity/revalidation 合同后才能升级为 `ACCEPTED_CANDIDATE`。

## 变更范围

仅包含：

- `backend/app/agent_capabilities/hosts/common.py`：task/operation/toolUseId/grantRevision host context、B03 policy bridge、value-free operation receipt。
- `backend/app/agent_capabilities/hosts/paths.py`：canonical absolute path、workspace containment、symlink/junction/reparse 检查、root/target identity。
- `backend/app/agent_capabilities/hosts/file.py`：verified read、glob、grep。
- `backend/app/agent_capabilities/hosts/mutation.py`：atomic write/patch、identity/revision recheck、delete、temporary-file cleanup。
- `backend/app/agent_capabilities/hosts/__init__.py` 与 `backend/tests/unit/agent_capabilities/test_hosts.py`。

未修改 command/network、bundled skill/security、公共 invocation/registry/glue 或 B03 source-of-truth 文件。

## Focused verification

使用仓库已有 `backend/.venv`，未安装新依赖：

```text
PYTHONPATH=backend /Users/yoligehude/Desktop/all/echo/backend/.venv/bin/python -m pytest -q backend/tests/unit/agent_capabilities/test_hosts.py -k contract
6 passed, 3 deselected

PYTHONPATH=backend /Users/yoligehude/Desktop/all/echo/backend/.venv/bin/python -m pytest -q backend/tests/unit/agent_capabilities/test_hosts.py -k real_mutation
3 passed, 6 deselected
```

Contract suite 覆盖：missing/revoked snapshot、cancel、grant revision mismatch、outside workspace、symlink、脱敏 receipt、glob/grep。真实 harness 覆盖：atomic write、patch、staged revoke、temporary cleanup、verified delete 和 symlink delete deny。测试使用显式真实 root identity 的 direct snapshot；compiler placeholder 路径不被伪造为 allow。

## Receipt / cleanup / provenance proof

- 每次 host attempt 都生成 receipt，包括 deny；receipt 绑定 `taskId`、`operationKey`、`toolUseId`、`grantRevision`、`policyRevision` 和 B03 deny code。
- receipt 只保存 target/pattern digest、计数和字节数；测试确认 raw file content、绝对路径和 tool result 不出现在 JSON 中。
- mutation 临时文件使用 task-host 专用 `.echodesk-b06p-*` 前缀；成功后 atomic replace，失败、cancel 或 revoke 在 `finally` 中删除，测试确认目录无残留。
- 不读取 HOME、global config、PATH fallback，不运行 hooks、runtime npm install 或外部进程；本 host 仅执行显式 workspace path I/O。

## Scope note

本文件是 A-owned evidence 摘要；未执行 F04 replay、全量回归、安装态验证或正式 package。主任务需在汇总时重新审计其他 subagent 的写集与最终 clean worktree。
