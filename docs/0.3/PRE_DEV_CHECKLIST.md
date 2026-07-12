# EchoDesk 0.3 开发前检查清单

日期：2026-07-09  
状态：冻结的历史准备记录，不作为当前实现或验收真源

## 1. 当前准备状态

| 项目 | 状态 | 说明 |
|---|---|---|
| 0.3 分支 | 已完成 | `codex/echodesk-0.3-workflow` |
| 0.2.50 基线识别 | 已完成 | `v0.2.50` / `e5574e9379e82f10057d5c84f401349c6f8e613b` [F-ECHO-001] |
| FactStore 初始化 | 已完成 | `_state/` 已创建 |
| FactStore replay | 已完成 | events applied: 1, facts: 2 |
| FactStore validate | 已完成 | errors: 0 |
| FactStore health-check | 已完成 | tentative/stale/expired/conflicts 全 OK |
| 0.3 架构文档 | 已完成 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 0.3 workflow 文档 | 已完成 | [WORKFLOWS.md](WORKFLOWS.md) |
| 0.3 数据契约文档 | 已完成 | [DATA_MODEL_AND_CONTRACTS.md](DATA_MODEL_AND_CONTRACTS.md) |
| 0.3 测试计划 | 已完成 | [TEST_PLAN.md](TEST_PLAN.md) |
| 0.3 开发计划 | 已完成 | [DEV_PLAN.md](DEV_PLAN.md) |

## 2. 开发前必须确认

已确认：

- Claude Code / AgentOS 作为正式 Agent Runner Workflow 保留。
- full access / bypass 权限不是问题。
- 问题集中在 workflow 闭环、状态追踪、重试、取消、超时、产物归档。
- 0.3 以 Desktop Pro / local-first 工作流为主。
- Artifact 进入后端 DB，不再只靠前端内存关联。
- Todo 执行态由 workflow/link 承载。
- Agent 产物并入统一 artifacts。
- 旧数据迁移不强行猜缺失关联。

## 3. 禁止直接开工的情况

出现以下情况必须暂停：

- 未跑 FactStore health-check。
- 未读本目录文档。
- 想绕过 Workflow Core 直接改某个按钮。
- 想新增临时 WS event 字段。
- 想继续用前端内存作为产物归属事实源。
- 想把 Agent 产物继续留在单独模型里。
- 想在没有测试计划的情况下改 schema。

## 4. PR-1 开工前命令

```bash
git status -sb
python3 /Users/yoligehude/Desktop/all/_platforms/principles/factstore/scripts/health_check.py --project echo --root /Users/yoligehude/Desktop/all

cd desktop
npm run typecheck
npm run build

cd ../backend
pytest tests/unit/test_health.py tests/unit/test_ws_endpoint.py -q
```

说明：

- 上述命令用于确认开发前基线，不代表 0.3 功能已实现。
- 若已有用户改动，必须先识别并保留。

## 5. PR-1 开工清单

- [ ] 读 [ARCHITECTURE.md](ARCHITECTURE.md)。
- [ ] 读 [DATA_MODEL_AND_CONTRACTS.md](DATA_MODEL_AND_CONTRACTS.md)。
- [ ] 新建 migration。
- [ ] 新建 workflow schema。
- [ ] 先写状态机单测。
- [ ] 再实现 WorkflowService。
- [ ] 再接 REST API。
- [ ] 最后接 event bus projection。

## 6. 验收口径

0.3 每个 PR 的最终回答必须包含：

- 改了哪些文件。
- 跑了哪些测试。
- 哪些测试没跑及原因。
- 对应 [DEV_PLAN.md](DEV_PLAN.md) 的哪个 PR 阶段。
- 是否引入 schema migration。
- 是否改变 REST/WS/IPC contract。

## 7. 当前未开始的开发项

以下内容尚未开发，这是正常状态：

- Workflow Core 代码。
- DB migration。
- Artifact DB 持久化。
- Todo workflow。
- Agent workflow 收编。
- outputs UI 重构。
- contract snapshot tests。
- version bump 到 `0.3.0-alpha.1`。

这些属于 [DEV_PLAN.md](DEV_PLAN.md) 的后续实现阶段。
