"""直连 /artifacts/generate/stream 探测某类产物的真实生成结果。

用于隔离 UI，单独判断后端 skill + 真 LLM 对某 artifact_type 是成功还是失败。
打印最终 done/error 事件与耗时。

用法：
    .venv/bin/python scripts/stress/probe_artifact.py pptx "生成PPT 人工智能行业分析"
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8769"


def probe(artifact_type: str, brief: str) -> int:
    t0 = time.monotonic()
    last_phase = ""
    final = None
    err = None
    with httpx.Client(timeout=400.0) as client:
        with client.stream(
            "POST",
            f"{BASE}/artifacts/generate/stream",
            json={"artifact_type": artifact_type, "brief": brief},
        ) as r:
            event = ""
            for line in r.iter_lines():
                if line.startswith("event: "):
                    event = line[len("event: ") :].strip()
                elif line.startswith("data: "):
                    data = line[len("data: ") :]
                    if event == "phase":
                        last_phase = data[:120]
                    elif event == "done":
                        final = data[:300]
                    elif event == "error":
                        err = data[:400]
    dt = time.monotonic() - t0
    if final:
        print(f"[{artifact_type}] OK in {dt:.1f}s  last_phase={last_phase}\n  done={final}")
        return 0
    print(f"[{artifact_type}] FAIL in {dt:.1f}s  last_phase={last_phase}\n  error={err}")
    return 1


if __name__ == "__main__":
    at = sys.argv[1] if len(sys.argv) > 1 else "pptx"
    bf = sys.argv[2] if len(sys.argv) > 2 else "生成PPT 人工智能行业分析"
    raise SystemExit(probe(at, bf))
