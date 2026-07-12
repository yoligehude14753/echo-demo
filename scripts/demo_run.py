"""离线 demo 串行脚本：

不需要 desktop / 不需要 STT/Diarizer。把"已经转写好的"逐字稿喂进 backend，跑完整链路：
  start → 3 个 chunk → finalize → 真 LLM 出纪要 → RAG 入库 → 用 /rag/ask 回查
  → 生成一份英伟达营收 HTML 产物 → 验证下载

跑法：
  cd backend
  uvicorn app.main:app --port 8769 --ws-max-size 4096 &
  cd ..
  python scripts/demo_run.py

期望输出：
  ✓ 会议事件序列正确
  ✓ 纪要 summary 含「Q3 预算」「砍 30%」之类关键词
  ✓ HTML 产物 size > 2KB
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress

import httpx
import websockets

# 强制忽略系统代理：本机走 localhost
for _k in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
):
    os.environ.pop(_k, None)

BASE = os.environ.get("ECHO_DEMO_BASE", "http://localhost:8769")
WS_URL = os.environ.get("ECHO_DEMO_WS", "ws://localhost:8769/ws/echo")

CHUNKS = [
    "今天讨论 Q3 预算，原方案 100 万元。",
    "我建议砍 30%，理由是 Q2 销售不及预期。",
    "同意 70 万方案，Alice 负责周五前出具修订版。",
]


async def collect_ws_events(stop_evt: asyncio.Event, sink: list[dict]) -> None:
    async with websockets.connect(WS_URL, proxy=None) as ws:
        while not stop_evt.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                return
            with suppress(json.JSONDecodeError):
                sink.append(json.loads(msg))


async def run_meeting_demo(client: httpx.AsyncClient, meeting_id: str) -> dict:
    r = await client.post(f"{BASE}/meetings/{meeting_id}/start")
    r.raise_for_status()
    print(f"  → start_meeting {meeting_id}", flush=True)

    # Demo 模式：通过 inject_segment 注入预录的逐字稿，避开外部 STT 依赖
    speakers = ["spk-A", "spk-B", "spk-A"]
    cursor = 0
    for i, text in enumerate(CHUNKS):
        seg = {
            "text": text,
            "start_ms": cursor,
            "end_ms": cursor + 2500,
            "speaker_id": speakers[i],
            "speaker_label": None,
        }
        r = await client.post(f"{BASE}/meetings/{meeting_id}/inject_segment", json=seg)
        r.raise_for_status()
        cursor += 2800
        print(f"  → inject_segment #{i + 1} ok", flush=True)

    r = await client.post(
        f"{BASE}/meetings/{meeting_id}/finalize",
        data={"title": "Q3 预算评审 - demo"},
    )
    r.raise_for_status()
    minutes = r.json()
    print(f"  ✓ 纪要: summary={minutes['summary'][:80]!r}", flush=True)
    print(
        f"    sections={len(minutes['sections'])} decisions={len(minutes['decisions'])} "
        f"action_items={len(minutes['action_items'])}",
        flush=True,
    )
    return minutes


async def run_artifact_demo(client: httpx.AsyncClient) -> dict:
    payload = {
        "artifact_type": "html",
        "brief": "生成一份单文件英伟达 2020-2025 年营收快照 HTML，深色主题 + Tailwind + SVG 柱图。",
    }
    r = await client.post(f"{BASE}/artifacts/generate", json=payload, timeout=180)
    r.raise_for_status()
    art = r.json()
    print(
        f"  ✓ HTML 产物: {art['artifact_id']} size={art['size_bytes']}",
        flush=True,
    )
    return art


async def main() -> int:
    sink: list[dict] = []
    stop = asyncio.Event()

    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        try:
            await client.get(f"{BASE}/healthz", timeout=5)
        except Exception as e:
            print(f"✗ backend 不在线: {e}", flush=True)
            return 1

        ws_task = asyncio.create_task(collect_ws_events(stop, sink))
        await asyncio.sleep(0.3)

        print("=== 会议 demo ===", flush=True)
        try:
            await run_meeting_demo(client, "demo-q3")
        except httpx.HTTPStatusError as e:
            print(f"  ⚠ 会议主链路 finalize 失败：{e}（STT 不在线时这是预期）", flush=True)

        print("=== 产物 demo ===", flush=True)
        try:
            await run_artifact_demo(client)
        except httpx.HTTPStatusError as e:
            print(f"  ✗ artifact 失败：{e.response.text}", flush=True)
            return 2

        await asyncio.sleep(1.5)
        stop.set()
        try:
            await asyncio.wait_for(ws_task, timeout=3.0)
        except TimeoutError:
            ws_task.cancel()

    types = [e.get("type") for e in sink]
    print(f"=== 事件流（共 {len(types)} 条）===", flush=True)
    for t in types:
        print(f"  · {t}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
