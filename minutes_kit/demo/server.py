"""minutes_kit demo dev server。

启动：
    python -m minutes_kit.demo  # 会路由到本文件的 main()

或者：
    cd minutes_kit && python demo/server.py

跟 echo backend 完全无关；端口 8810 隔离。

页面 (GET /)：粘贴 transcript 文本 + 选项 → 提交 → 直接看 preview.html + 下载 docx。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# 允许从 demo/ 直接跑：把项目根加入 path
_DEMO_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _DEMO_DIR.parent
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from minutes_kit.llm_client import OpenAIClient  # noqa: E402
from minutes_kit.orchestrator import MinutesGenerationError, generate_minutes  # noqa: E402
from minutes_kit.transcript_io import parse_transcript_text  # noqa: E402


OUT_BASE = _PROJECT_ROOT / "out" / "demo_runs"
OUT_BASE.mkdir(parents=True, exist_ok=True)


app = FastAPI(title="minutes_kit demo")
app.mount("/runs", StaticFiles(directory=str(OUT_BASE)), name="runs")


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>minutes_kit · demo</title>
<style>
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
       max-width: 900px; margin: 32px auto; padding: 0 16px; line-height: 1.6; }
h1 { margin-bottom: 4px; }
p.lead { color: #6b7280; margin-top: 0; }
textarea { width: 100%; min-height: 280px; font-family: ui-monospace, monospace; font-size: 14px;
           padding: 12px; border-radius: 8px; border: 1px solid #ccc; }
.row { display: flex; gap: 12px; margin: 12px 0; flex-wrap: wrap; }
.row label { display: flex; flex-direction: column; flex: 1 1 200px; font-size: 13px; }
input[type=text] { padding: 8px 10px; border-radius: 6px; border: 1px solid #ccc; }
button { background: #2563eb; color: white; padding: 10px 20px; border: none; border-radius: 8px;
         font-size: 15px; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: wait; }
.runs { margin-top: 32px; }
.run-card { display: flex; gap: 12px; align-items: center; padding: 8px 0;
            border-bottom: 1px solid #eee; }
.run-card a { color: #2563eb; text-decoration: none; }
.hint { font-size: 12px; color: #6b7280; }
pre.sample { background: #f6f8fa; padding: 12px; border-radius: 6px; font-size: 12px;
             max-height: 200px; overflow: auto; }
</style>
</head>
<body>
<h1>minutes_kit · demo</h1>
<p class="lead">粘贴会议转录 → 一键生成 HTML 预览 + Word 文档 + Mermaid 流程图。</p>

<form method="post" action="/generate" id="genForm">
  <div class="row">
    <label>标题提示（可选）
      <input type="text" name="title_hint" placeholder="例：周三例会">
    </label>
    <label>参会人（逗号分隔，可选）
      <input type="text" name="participants" placeholder="例：A,B,C">
    </label>
  </div>
  <div class="row">
    <label style="flex-direction: row; align-items: center; gap: 8px;">
      <input type="checkbox" name="use_claude" value="1">
      启用 Claude skill 高品质 docx（需 ``claude`` binary）
    </label>
  </div>
  <textarea name="transcript_text" id="transcript" placeholder="支持格式：
[10:00:00] A: 我们今天讨论 Word 模板规范
[10:00:15] B: 同意 Word 用模板，Excel 列待办
[10:01:00] C: 那我负责 Excel 部分
..."></textarea>
  <div class="row">
    <button type="submit" id="submitBtn">生成纪要</button>
    <span class="hint" id="status"></span>
  </div>
</form>

<details class="runs">
<summary><strong>历史产物</strong>（refresh 刷新）</summary>
<div id="runList">加载中...</div>
</details>

<script>
fetch('/api/runs').then(r => r.json()).then(data => {
  var html = data.runs.length === 0
    ? '<p class="hint">暂无历史，先在上面提交一次。</p>'
    : data.runs.map(r =>
        '<div class="run-card"><span>' + r.run_id + '</span>'
        + '<a href="' + r.preview_url + '" target="_blank">preview.html</a>'
        + (r.docx_url ? ' · <a href="' + r.docx_url + '">minutes.docx</a>' : '')
        + ' · <a href="' + r.data_url + '" target="_blank">data.json</a>'
        + '</div>'
      ).join('');
  document.getElementById('runList').innerHTML = html;
});

document.getElementById('genForm').addEventListener('submit', function (e) {
  document.getElementById('submitBtn').disabled = true;
  document.getElementById('status').textContent = '生成中（30-90 秒，看 LLM 速度）...';
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.post("/generate")
async def generate_endpoint(
    transcript_text: str = Form(...),
    title_hint: str = Form(""),
    participants: str = Form(""),
    use_claude: str = Form(""),
) -> JSONResponse:
    """提交 transcript 生成纪要。"""
    turns = parse_transcript_text(transcript_text)
    if not turns:
        raise HTTPException(status_code=400, detail="transcript 解析后为空")

    run_id = uuid.uuid4().hex[:12]
    out_dir = OUT_BASE / run_id

    participants_list = [p.strip() for p in participants.split(",") if p.strip()] or None
    use_claude_skill = bool(use_claude.strip())

    logger.info(
        f"[demo] generate run_id={run_id} turns={len(turns)} "
        f"use_claude={use_claude_skill}"
    )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="未设置 OPENAI_API_KEY（或 OPENAI_BASE_URL 指向兼容服务）",
        )

    llm = OpenAIClient()

    try:
        result = await generate_minutes(
            transcript=turns,
            llm_client=llm,
            out_dir=out_dir,
            participants=participants_list,
            title_hint=title_hint.strip() or None,
            minutes_id=run_id,
            use_claude_skill=use_claude_skill,
        )
    except MinutesGenerationError as exc:
        raise HTTPException(status_code=500, detail=f"生成失败: {exc}") from exc

    return JSONResponse({
        "run_id": run_id,
        "preview_url": f"/runs/{run_id}/preview.html",
        "docx_url": f"/runs/{run_id}/minutes.docx" if result.docx_path else None,
        "data_url": f"/runs/{run_id}/data.json",
        "warnings": result.warnings,
        "docx_generator": result.docx_generator,
    })


@app.get("/api/runs")
async def list_runs() -> JSONResponse:
    runs = []
    if OUT_BASE.exists():
        for d in sorted(OUT_BASE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            preview = d / "preview.html"
            docx = d / "minutes.docx"
            data = d / "data.json"
            if not preview.exists() and not data.exists():
                continue
            runs.append({
                "run_id": d.name,
                "preview_url": f"/runs/{d.name}/preview.html" if preview.exists() else None,
                "docx_url": f"/runs/{d.name}/minutes.docx" if docx.exists() else None,
                "data_url": f"/runs/{d.name}/data.json" if data.exists() else None,
            })
    return JSONResponse({"runs": runs[:50]})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "out_base": str(OUT_BASE),
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
    })


def main() -> None:
    """启动入口（被 minutes_kit/demo/__main__.py 调用）。"""
    host = os.environ.get("MINUTES_KIT_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("MINUTES_KIT_DEMO_PORT", "8810"))
    logger.info(f"[demo] starting at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
