"""EchoDesk 运行态冒烟回归。

目标：在已启动的 EchoDesk backend 上，用真实 HTTP/SSE 路径验证关键能力：

1. /healthz                         后端存活
2. /healthz/full                    DB/远程探针结构可返回
3. /recap/today                     今日回顾与结构化 todos 字段可返回
4. /agent/run                       Agent SSE 至少产出 delta/final/done，记录首字延迟
5. /artifacts/generate/stream       可选：真实产物生成（--artifact-type 才启用）

默认不生成产物，避免每次 smoke 都烧模型额度和落文件；CI/人工验收需要时显式加：

    .venv/bin/python scripts/stress/runtime_smoke.py --artifact-type html
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8769"
DEFAULT_AGENT_QUESTION = "用一句话回答：EchoDesk 当前运行是否正常？"
DEFAULT_ARTIFACT_BRIEF = (
    "生成一个简洁 HTML one-pager，主题是 EchoDesk 运行态冒烟测试，"
    "包含标题、三条检查项、一个结论。"
)


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    latency_ms: int
    detail: dict[str, Any]


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _print_check(check: Check) -> None:
    status = "OK" if check.ok else "FAIL"
    print(
        f"[{status}] {check.name} {check.latency_ms}ms "
        + json.dumps(check.detail, ensure_ascii=False, sort_keys=True)
    )


def _sse_events(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    """把 ``event: x`` / ``data: json`` SSE 行流解析成事件元组。"""
    event = "message"
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip() or "message"
            continue
        if line.startswith("data:"):
            data_raw = line.split(":", 1)[1].strip()
            try:
                data = json.loads(data_raw)
            except json.JSONDecodeError:
                data = data_raw
            yield event, data


def check_health(client: httpx.Client, base: str) -> Check:
    t0 = time.monotonic()
    try:
        r = client.get(f"{base}/healthz", timeout=5.0)
        ok = r.status_code == 200 and (r.json().get("status") == "ok")
        return Check(
            "healthz",
            ok,
            _ms_since(t0),
            {"status_code": r.status_code, "body": r.json() if r.content else None},
        )
    except Exception as exc:
        return Check("healthz", False, _ms_since(t0), {"error": str(exc)})


def check_health_full(client: httpx.Client, base: str) -> Check:
    t0 = time.monotonic()
    try:
        r = client.get(f"{base}/healthz/full", timeout=10.0)
        data = r.json()
        ok = (
            r.status_code == 200
            and data.get("backend", {}).get("ok") is True
            and isinstance(data.get("db"), dict)
            and isinstance(data.get("remote"), dict)
        )
        return Check(
            "healthz_full",
            ok,
            _ms_since(t0),
            {
                "status_code": r.status_code,
                "db_ok": data.get("db", {}).get("ok"),
                "remote_keys": sorted((data.get("remote") or {}).keys()),
            },
        )
    except Exception as exc:
        return Check("healthz_full", False, _ms_since(t0), {"error": str(exc)})


def check_recap(client: httpx.Client, base: str) -> Check:
    t0 = time.monotonic()
    try:
        r = client.get(f"{base}/recap/today", timeout=90.0)
        data = r.json()
        todos = data.get("todos")
        ok = (
            r.status_code == 200
            and isinstance(data.get("empty"), bool)
            and isinstance(todos, list)
            and "recap_markdown" in data
        )
        return Check(
            "recap_today",
            ok,
            _ms_since(t0),
            {
                "status_code": r.status_code,
                "empty": data.get("empty"),
                "todos": len(todos or []),
                "ambient": data.get("n_ambient_segments"),
                "meetings": data.get("n_meetings"),
            },
        )
    except Exception as exc:
        return Check("recap_today", False, _ms_since(t0), {"error": str(exc)})


def check_agent(client: httpx.Client, base: str, question: str) -> Check:
    t0 = time.monotonic()
    counts: dict[str, int] = {}
    first_delta_ms: int | None = None
    final_seen = False
    done_seen = False
    error_payload: Any = None
    try:
        with client.stream(
            "POST",
            f"{base}/agent/run",
            json={"question": question, "max_iterations": 3},
            timeout=120.0,
        ) as r:
            for event, data in _sse_events(r.iter_lines()):
                counts[event] = counts.get(event, 0) + 1
                if event == "delta" and first_delta_ms is None:
                    first_delta_ms = _ms_since(t0)
                elif event == "final":
                    final_seen = True
                elif event == "done":
                    done_seen = True
                    break
                elif event == "error":
                    error_payload = data
                    break
        ok = r.status_code == 200 and final_seen and done_seen and counts.get("delta", 0) > 0
        return Check(
            "agent_run",
            ok,
            _ms_since(t0),
            {
                "status_code": r.status_code,
                "events": counts,
                "first_delta_ms": first_delta_ms,
                "error": error_payload,
            },
        )
    except Exception as exc:
        return Check("agent_run", False, _ms_since(t0), {"error": str(exc)})


def check_artifact(
    client: httpx.Client,
    base: str,
    artifact_type: str,
    brief: str,
) -> Check:
    t0 = time.monotonic()
    counts: dict[str, int] = {}
    done: dict[str, Any] | None = None
    error_payload: Any = None
    last_phase: Any = None
    try:
        with client.stream(
            "POST",
            f"{base}/artifacts/generate/stream",
            json={"artifact_type": artifact_type, "brief": brief},
            timeout=500.0,
        ) as r:
            for event, data in _sse_events(r.iter_lines()):
                counts[event] = counts.get(event, 0) + 1
                if event == "phase":
                    last_phase = data
                elif event == "done":
                    done = data if isinstance(data, dict) else {"raw": data}
                    break
                elif event == "error":
                    error_payload = data
                    break
        ok = (
            r.status_code == 200
            and done is not None
            and bool(done.get("artifact_id"))
            and int(done.get("size_bytes") or 0) > 0
        )
        return Check(
            f"artifact_{artifact_type}",
            ok,
            _ms_since(t0),
            {
                "status_code": r.status_code,
                "events": counts,
                "artifact_id": (done or {}).get("artifact_id"),
                "size_bytes": (done or {}).get("size_bytes"),
                "last_phase": last_phase,
                "error": error_payload,
            },
        )
    except Exception as exc:
        return Check(f"artifact_{artifact_type}", False, _ms_since(t0), {"error": str(exc)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EchoDesk runtime smoke checks")
    parser.add_argument("--base", default=DEFAULT_BASE, help="backend base URL")
    parser.add_argument("--skip-agent", action="store_true", help="skip /agent/run real LLM check")
    parser.add_argument("--question", default=DEFAULT_AGENT_QUESTION, help="agent smoke question")
    parser.add_argument("--artifact-type", help="optional artifact type to generate, e.g. html/pptx")
    parser.add_argument("--artifact-brief", default=DEFAULT_ARTIFACT_BRIEF, help="artifact brief")
    args = parser.parse_args(argv)

    checks: list[Check] = []
    with httpx.Client() as client:
        checks.append(check_health(client, args.base))
        checks.append(check_health_full(client, args.base))
        checks.append(check_recap(client, args.base))
        if not args.skip_agent:
            checks.append(check_agent(client, args.base, args.question))
        if args.artifact_type:
            checks.append(check_artifact(client, args.base, args.artifact_type, args.artifact_brief))

    for check in checks:
        _print_check(check)

    failed = [c.name for c in checks if not c.ok]
    if failed:
        print(f"runtime smoke failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
