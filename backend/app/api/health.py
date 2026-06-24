"""扩展健康检查：/healthz/full 返回 backend + db + 各远程依赖最新探针。

P1.4（独立产品 Phase 1）：UI 状态栏 4 个 pill 的数据源；诊断包 / 故障定位
都从这里读。

实现：
- 后台 task 每 30s 用 TCP socket connect 探一次远程依赖（不发实际 HTTP，避免
  对远程加负担、产生噪音、消耗 quota）
- 探针结果缓存到内存；/healthz/full 直接读 cache
- 第一次启动时同步探一遍，让首次调用就有数据

远程探针名规范（前端 pill 引用同一份）：
  heyi_stt_firered    eight :8090 FireRedASR2-AED
  heyi_tts_qwen3      eight :8094 qwen3 TTS
  heyi_llm_fast       eight :7860 qwen3.5-9b-local vLLM
  yunwu_llm_main      yunwu.ai MiniMax-M2.7（无 key 时 ok=null reason=no_api_key）
  tavily              api.tavily.com（无 key 时 ok=null reason=no_api_key）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from app import __version__
from app.config import Settings, get_settings

logger = logging.getLogger("echodesk.health")
router = APIRouter(tags=["meta"])

_BOOT_TIME = time.monotonic()
_PROBE_INTERVAL_S = 30.0
_PROBE_TIMEOUT_S = 8.0
_PROBE_FAILURE_GRACE_COUNT = 3


@dataclass
class ProbeResult:
    """ok 三态：True 探通 / False 探失败 / None 不适用（如缺 api key）。"""

    ok: bool | None = None
    latency_ms: float | None = None
    error: str | None = None
    reason: str | None = None
    checked_at: float = field(default_factory=time.time)


_cache: dict[str, ProbeResult] = {}
_failure_counts: dict[str, int] = {}
_prober_task: asyncio.Task[None] | None = None


def _host_port_from_url(url: str, default_port: int = 80) -> tuple[str, int]:
    """从 base_url（http(s)://host:port 或 host:port）拆 (host, port)。"""
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else default_port


async def _probe_tcp(host: str, port: int) -> ProbeResult:
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        sock = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: socket.create_connection((host, port), _PROBE_TIMEOUT_S)
            ),
            timeout=_PROBE_TIMEOUT_S + 1.0,
        )
        with contextlib.suppress(OSError):
            sock.close()
        return ProbeResult(
            ok=True,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
        )
    except TimeoutError:
        return ProbeResult(ok=False, error="timeout")
    except OSError as e:
        return ProbeResult(ok=False, error=str(e)[:200])
    except Exception as e:  # pragma: no cover
        return ProbeResult(ok=False, error=f"{type(e).__name__}: {e}"[:200])


async def _probe_all(settings: Settings) -> dict[str, ProbeResult]:
    probes: list[tuple[str, asyncio.Future[ProbeResult] | asyncio.Task[ProbeResult]]] = []
    host, port = _host_port_from_url(settings.stt_firered_url)
    probes.append(("heyi_stt_firered", asyncio.ensure_future(_probe_tcp(host, port))))

    if settings.tts_enabled:
        host, port = _host_port_from_url(settings.tts_qwen3_url)
        probes.append(("heyi_tts_qwen3", asyncio.ensure_future(_probe_tcp(host, port))))
    else:
        probes.append(
            (
                "heyi_tts_qwen3",
                asyncio.ensure_future(
                    asyncio.sleep(0, ProbeResult(ok=None, reason="tts_disabled"))
                ),
            )
        )

    host, port = _host_port_from_url(settings.llm_fast_base_url)
    probes.append(("heyi_llm_fast", asyncio.ensure_future(_probe_tcp(host, port))))

    if not settings.yunwu_open_key:
        probes.append(
            (
                "yunwu_llm_main",
                asyncio.ensure_future(asyncio.sleep(0, ProbeResult(ok=None, reason="no_api_key"))),
            )
        )
    else:
        host, port = _host_port_from_url(settings.llm_main_base_url, 443)
        probes.append(("yunwu_llm_main", asyncio.ensure_future(_probe_tcp(host, port))))

    if not settings.tavily_api_key:
        probes.append(
            (
                "tavily",
                asyncio.ensure_future(asyncio.sleep(0, ProbeResult(ok=None, reason="no_api_key"))),
            )
        )
    else:
        probes.append(("tavily", asyncio.ensure_future(_probe_tcp("api.tavily.com", 443))))

    values = await asyncio.gather(*(probe for _, probe in probes))
    return {name: value for (name, _), value in zip(probes, values, strict=True)}


def _apply_probe_results(results: dict[str, ProbeResult]) -> None:
    """Update probe cache with a small grace window for flaky tailnet probes.

    eight occasionally accepts real STT/TTS requests while a lightweight TCP probe
    times out. Flipping the status pill red on the first missed probe makes the UI
    look broken even when capture is still working, so keep the last known-good
    status until the same dependency fails several rounds in a row.
    """
    stale_names = set(_cache) - set(results)
    for name in stale_names:
        _cache.pop(name, None)
        _failure_counts.pop(name, None)

    for name, result in results.items():
        previous = _cache.get(name)
        if result.ok is False:
            failures = _failure_counts.get(name, 0) + 1
            _failure_counts[name] = failures
            if previous and previous.ok is True and failures < _PROBE_FAILURE_GRACE_COUNT:
                _cache[name] = ProbeResult(
                    ok=True,
                    latency_ms=previous.latency_ms,
                    reason=f"last_ok_retained_after_{result.error or 'probe_failure'}",
                    checked_at=result.checked_at,
                )
                continue
        else:
            _failure_counts[name] = 0
        _cache[name] = result


async def _prober_loop(settings: Settings) -> None:
    """每 30s 跑一轮探针；异常不应该让循环退出。"""
    while True:
        try:
            results = await _probe_all(settings)
            _apply_probe_results(results)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("prober iteration failed: %s", e)
        try:
            await asyncio.sleep(_PROBE_INTERVAL_S)
        except asyncio.CancelledError:
            raise


async def start_prober() -> None:
    """lifespan 启动时调用。同步跑首轮，确保首次 GET /healthz/full 有数据。"""
    global _prober_task  # noqa: PLW0603
    if _prober_task is not None:
        return
    settings = get_settings()
    try:
        first = await _probe_all(settings)
        _apply_probe_results(first)
        n_ok = sum(1 for r in _cache.values() if r.ok is True)
        n_fail = sum(1 for r in _cache.values() if r.ok is False)
        n_na = sum(1 for r in _cache.values() if r.ok is None)
        logger.info(
            "prober first round: %d ok, %d fail, %d n/a (interval=%ds)",
            n_ok,
            n_fail,
            n_na,
            int(_PROBE_INTERVAL_S),
        )
    except Exception as e:
        logger.warning("prober first round failed: %s", e)
    _prober_task = asyncio.create_task(_prober_loop(settings))


async def stop_prober() -> None:
    global _prober_task  # noqa: PLW0603
    if _prober_task is None:
        return
    _prober_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _prober_task
    _prober_task = None


def _db_status(settings: Settings) -> dict[str, Any]:
    # .env 里 DB_PATH=~/.echodesk/... 时 pydantic 不自动 expand `~`，
    # 跟 repo/__init__.py、ambient_capture、bm25 等保持一致 defensive expand
    from pathlib import Path

    p = Path(settings.db_path).expanduser()
    if not p.exists():
        return {"ok": False, "error": "db file missing", "path": str(p)}
    try:
        size_mb = p.stat().st_size / (1024 * 1024)
        return {"ok": True, "size_mb": round(size_mb, 3), "path": str(p)}
    except OSError as e:
        return {"ok": False, "error": str(e), "path": str(p)}


def _probe_to_dict(probe: ProbeResult) -> dict[str, Any]:
    """删 None 字段（除 ok 永远保留）让 JSON 更干净。"""
    d = asdict(probe)
    return {k: v for k, v in d.items() if v is not None or k == "ok"}


@router.get("/healthz/full")
async def healthz_full(
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """完整健康：backend + db + 5 个远程依赖 + mic（mic 由前端补）。"""
    return {
        "backend": {
            "ok": True,
            "version": __version__,
            "port": settings.port,
            "uptime_s": round(time.monotonic() - _BOOT_TIME, 1),
        },
        "db": _db_status(settings),
        "remote": {name: _probe_to_dict(probe) for name, probe in _cache.items()},
        # mic 权限只能从 Electron renderer 探（navigator.permissions），后端永远 unknown
        # 前端 status pill (P2.1) 会自己合并 navigator 数据
        "mic": {"ok": "unknown"},
    }
