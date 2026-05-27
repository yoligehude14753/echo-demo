# desktop

EchoDesk 的桌面端（Electron + React + TypeScript）。

> 当前为 placeholder：将在 **Sprint 4（前端 + WebSocket，PR-10~12）** 中按 `docs/DEV_PLAN.md` 初始化。
>
> 复用 echo 现有 `desktop/` 代码作为基线，重点改造：
> - 主界面：清单式（列表）会议+对话流，而非单人对话
> - WS 协议：对齐 backend 新 schema（`MeetingMinutes` / `ArtifactResult` / `TranscriptSegment`）
> - 产物展示：PPT / Word / Excel / HTML 内嵌预览（PPT 用 PDF 转换，HTML 直接 iframe）

## 启动（待 Sprint 4）

```bash
cd desktop
npm install
npm run dev
```
