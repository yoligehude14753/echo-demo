# B06P main-task evidence

状态：`BLOCKED_CONTRACT_CHANGE_REQUESTED`

## 基线、边界与 subagent

- base：`e7bacd136f77cfeba157af0dc6151b692b43ac05`（B03 `ACCEPTED`）。
- compatibility baseline：`492053c53441793c220f3b8e1dd231f1faea6e42`。
- F04 evidence：`db57ddefc95c494c3785659db89befe6d8cf9c94`，未重放 fake tool/cancel/mismatch。
- 冻结合同：v1；B06P 吸收旧 B06+B09，未创建独立 B09。
- subagent 恰好 3 个，均未派生子 agent：
  - `file-mutation-hosts`：`backend/app/agent_capabilities/hosts/**` 与 `test_hosts.py`；
  - `command-network-hosts`：`command_network_hosts.py` 与 `test_command_network_hosts.py`；
  - `skill-and-security-verification`：`skill_host.py`、`test_skill_host.py` 与 C evidence。
- 主任务写集：`host_runtime.py`、`test_host_runtime.py`、package export 和本 evidence；只提供共享 invocation/receipt/cancel/registry glue。

## Focused verification

1. touched compile/lint/typecheck：compile 与 ruff 通过；`host_runtime.py` 严格 mypy 无自身错误。其依赖的 B03 既有跨模块 baseline errors 未改动。
2. 主任务 table-driven contract suite：`pytest -q backend/tests/unit/agent_capabilities/test_host_runtime.py` → `7 passed`。
3. 真实 host harness：
   `pytest -q backend/tests/unit/agent_capabilities/test_hosts.py backend/tests/unit/agent_capabilities/test_command_network_hosts.py backend/tests/unit/agent_capabilities/test_skill_host.py` → `33 passed`。

## 证明摘要

- file host：canonical containment、symlink/reparse deny、atomic write/patch/delete、revision/cancel/revoke recheck、临时文件清理；A evidence 为 `6 passed` contract + `3 passed` real mutation。
- command/network host：argv-only、无 shell string 拼接、显式 executable/cwd verification、process tree cancel/timeout/revoke cleanup、每跳 DNS/redirect revalidation、private-address deny；B evidence 为 `29 passed`。
- skill host：signed manifest、canonical manifest/resource SHA-256、显式 bundle root、platform table、provenance、registered handler、receipt hash/redaction；C evidence 为 `17 passed`。
- shared glue：未知 host、binding mismatch、revision mismatch、取消幂等和 revoke token 均 fail closed；receipt 只保存 identity、revision、code、digest/counter 和 redacted metadata。
- unsupported scan：P0 hooks、global config、HOME/PATH discovery、runtime npm/pnpm/yarn/pip install 均只返回 `UNSUPPORTED_P0_FAIL_CLOSED`；没有运行时安装、PATH/HOME 搜索或任意 hook 执行。

## 阻塞与裁定

A 发现 B03 compiler 生成的 workspace identity 为 `host-verification-required` placeholder，无法安全匹配真实 root `dev:ino` identity。A 没有修改 B03 事实源，也没有猜测放行；真实 file allow path 保持 fail closed。因此本批只能裁定 `BLOCKED_CONTRACT_CHANGE_REQUESTED`，不能升级为 `ACCEPTED_CANDIDATE`，直到 B03 提供 host-bound identity/revalidation 合同。

未运行全量回归、F04 replay、安装态矩阵或正式 package；未 push、PR 或发布。
