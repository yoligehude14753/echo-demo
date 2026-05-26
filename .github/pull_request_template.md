<!--
PR 必填模板（alld 09-cicd.mdc §PR Review）。
缺一项不得发起 Review。请勿删除小节标题。
-->

## 背景（Why）
<!-- 引用 task-id / issue / DEV_PLAN 链接 -->

## 变更（What）
<!-- 一句话总结 + 关键文件列表 -->

## 验证（How Verified）
<!--
- 本地如何验证业务目标达成
- 操作步骤 + 实际观察结果
- 自动化测试新增/修改清单
- Happy Path + 关键 Sad Path 验证情况
-->

## 截图 / 录屏（UI 变更必填）
<!-- 改动前 vs 改动后；关键交互的短录屏；否则显式写"无 UI 变更" -->

## 风险与回滚（Risk & Rollback）
<!-- 可能影响的功能 / 数据 / 用户；如何回滚；是否需 Feature Flag -->

## 相关
<!-- 关联 PR、ADR、Issue、文档 -->

---

### 合并 Checklist（必须全部勾选）

- [ ] 业务目标三问通过：主路径可用 / 失败路径有反馈 / 状态集完整
- [ ] CI `check` job 绿
- [ ] Self-Review 已完成
- [ ] AI Reviewer 首过完成，P0/P1 建议已处理
- [ ] UI 变更已附截图/录屏（或显式说明无 UI 变更）
- [ ] 分支领先 main ≤ 2 天
- [ ] PR 规模 ≤ 400 行 diff（超出请说明理由）
- [ ] commit message 含 task-id
