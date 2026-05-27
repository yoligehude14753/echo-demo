# Echo Demo · 跑通指南

> 目标：从零到一，10 分钟内跑出完整的「会议 → 纪要 → 产物」演示。
>
> 验收：会议清单出现一个 demo 会议，转写流有 3 段对话，纪要面板显示真实 LLM 输出的 Q3 预算决议，HTML 产物可下载并 iframe 预览。

---

## 0. 前置依赖

```bash
# 系统：macOS 13+ / Linux x86_64
# Python 3.12+（项目 CI 用 3.12），Node 20+
# 一个能调通 Yunwu 的 YUNWU_OPEN_KEY（M2.7 主通道）

cd echodesk
cp .env.example .env
$EDITOR .env    # 填 YUNWU_OPEN_KEY=sk-xxxx 和 TAVILY_API_KEY=tvly-xxxx
```

---

## 1. backend 启动

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8765
```

期望：
- 控制台打印 `echodesk 启动: version=0.1.0 llm_main=MiniMax-M2.7 ...`
- `curl http://localhost:8765/healthz` → `{"status":"ok"}`
- `curl http://localhost:8765/bootstrap` 输出能力开关

> 端口 8765 被占时换其它端口，前端 dev 也对应改 `VITE_API_TARGET`。

---

## 2. desktop 启动

```bash
cd desktop
npm install
npm run dev   # 5173
```

或指向自定义后端端口：

```bash
VITE_API_TARGET=http://localhost:8766 npm run dev
```

访问 http://localhost:5173 → 应看到 3 列布局，右上角显示「已连接」。

---

## 3. 触发一次完整会议演示

无 STT 服务也能演示（用预录逐字稿注入 pipeline）：

```bash
# 在 backend / desktop 都已启动的前提下
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
python scripts/demo_run.py
```

期望输出：

```
=== 会议 demo ===
  → start_meeting demo-q3
  → inject_segment #1 ok
  → inject_segment #2 ok
  → inject_segment #3 ok
  ✓ 纪要: summary='Q3 预算评审决定将原方案 100 万元缩减至 70 万元，降幅 30%...'
    sections=1 decisions=1 action_items=1
=== 产物 demo ===
  ✓ HTML 产物: html-xxxxxxxx size=60000+
=== 事件流（共 8 条）===
  · meeting.started · meeting.segment ×3 · meeting.ended · minutes.ready
  · artifact.generating · artifact.ready
```

打开 http://localhost:5173 应能看到（事件总线带 200 事件 replay buffer，后开浏览器也能看到刚发生的演示）：
- 左侧会议清单中 `demo-q3` 进入「已结束」
- 转写流显示 3 段对话，按时间戳 + 说话人色彩区分
- 纪要面板显示 summary / sections / decisions / action_items
- 产物面板列出 1 个 HTML 产物，点击可 iframe 预览，右上角下载

---

## 4. 真音频 → 真转写（可选，需 STT 服务）

前端麦克风在 App 根挂载 `useEchoCapture()`，**持续采集、无手动开关**；会议写入由 `@开始会议` / `@总结会议` 控制。

如果要演示「持续采集→ASR→纪要」完整链路，需要：

```bash
# STT: sensevoice_gpu @ heyi-bj :8093
curl http://100.87.251.9:8093/v1/audio/transcriptions ...
# TTS: cosyvoice @ heyi-bj :8094
# Diarizer: 本地 speechbrain（首次启动会拉 ~80MB 模型缓存）
```

前端没有单独的「开始录音」按钮（CaptureSession 应用启动即采集）；本地测试 chunk 可用：

```bash
# 30s 切片喂 chunk 端点
ffmpeg -i meeting.wav -f wav -ar 16000 -ac 1 - | split -b 960000 - chunk_
for f in chunk_*; do
  curl -X POST http://localhost:8765/meetings/real-1/chunk \
    -F "audio=@${f}" -F "sample_rate=16000"
done
curl -X POST http://localhost:8765/meetings/real-1/finalize \
  -F "title=真实会议"
```

---

## 5. 自动化验收

```bash
cd backend
pytest tests/unit tests/arch                  # 88 单测，秒级
pytest tests/integration -m integration       # 真 LLM/RAG/Web E2E（需 .env 凭据）
```

期望：
- unit + arch：88 passed
- integration（含 yunwu + tavily 等真服务）：4-6 passed（依条件）

---

## 6. Demo 录屏（自动化生成）

```bash
# 前置：backend (:8769) + dev server (:5173) 已运行
cd desktop
npm run demo:record
```

产物：
- `desktop/test-results/demo-recording/**/video.webm`（~3MB / 1280×800 / 25fps）
- `desktop/test-results/demo-recording/final-frame.png`（结尾截图）

转 mp4：

```bash
ffmpeg -i video.webm -c:v libx264 -crf 20 -preset slow -pix_fmt yuv420p out.mp4
```

录屏覆盖：连接 → 拖入 markdown 入库 → 工作区状态栏 → @开始会议 → @查 意图分类 → @生成 HTML 派发反馈。
LLM 慢时跳过等待 artifact 完成（仍录到派发反馈，结尾停留展示）。

---

## 7. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `chunk` 返回 502 | STT 不在线 | 用 `inject_segment` 代替（demo_run.py 默认走这条） |
| WS 断线重连不停 | 后端端口不对 | 检查 `VITE_API_TARGET` 与 backend 实际端口 |
| Minutes JSON parse failed | M2.7 思考链泄漏 | 已自动剥 `<think>...</think>`；若仍失败重发，或检查 prompt |
| `ImportError: python-socks` | 系统 SOCKS 代理污染 | 在跑脚本前 `unset *_PROXY`，或脚本里 `trust_env=False` |
| ruff/mypy CI 触发 500 | GitHub Actions 平台问题 | 本地 `ruff check && mypy && pytest` 全过即可，admin merge |
