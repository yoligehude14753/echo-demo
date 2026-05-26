"""轻量 demo：只跑会议主线，不跑慢的 artifact。

启动 backend 后跑：
    python scripts/demo_run_quick.py demo-q4
将 demo-q4 注入 3 段逐字稿，finalize 出真 LLM 纪要。
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

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

CHUNKS = [
    "今天讨论 Q3 预算，原方案 100 万元。",
    "我建议砍 30%，理由是 Q2 销售不及预期。",
    "同意 70 万方案，Alice 负责周五前出具修订版。",
]


async def main(meeting_id: str) -> int:
    speakers = ["spk-A", "spk-B", "spk-A"]
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        try:
            await client.get(f"{BASE}/healthz", timeout=5)
        except Exception as e:
            print(f"✗ backend 不在线: {e}", flush=True)
            return 1
        print(f"=== 注入 {meeting_id} ===", flush=True)
        r = await client.post(f"{BASE}/meetings/{meeting_id}/start")
        r.raise_for_status()
        cursor = 0
        for i, text in enumerate(CHUNKS):
            seg = {
                "text": text,
                "start_ms": cursor,
                "end_ms": cursor + 2500,
                "speaker_id": speakers[i],
                "speaker_label": None,
            }
            r = await client.post(
                f"{BASE}/meetings/{meeting_id}/inject_segment", json=seg
            )
            r.raise_for_status()
            print(f"  → seg #{i + 1}", flush=True)
            cursor += 2800
        r = await client.post(
            f"{BASE}/meetings/{meeting_id}/finalize",
            data={"title": "Q3 预算评审 - demo"},
        )
        r.raise_for_status()
        m = r.json()
        print(f"✓ summary: {m['summary'][:80]!r}", flush=True)
        print(
            f"  sections={len(m['sections'])} decisions={len(m['decisions'])} "
            f"action_items={len(m['action_items'])}"
        )
    return 0


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "demo-q4"
    sys.exit(asyncio.run(main(mid)))
