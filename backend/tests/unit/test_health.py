"""health.py 单测：URL 解析、probe 序列化、db 状态、prober lifecycle。

P1.4（独立产品 Phase 1）：/healthz/full 是 UI status pill + 诊断包的数据
源，回归不能破。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from app.api.health import (
    ProbeResult,
    _db_status,
    _host_port_from_url,
    _probe_to_dict,
    healthz_full,
    start_prober,
    stop_prober,
)
from app.config import Settings


@pytest.mark.unit
class TestHostPortFromUrl:
    @pytest.mark.parametrize(
        "url,want",
        [
            ("http://100.76.3.59:8090", ("100.76.3.59", 8090)),
            ("https://yunwu.ai/v1", ("yunwu.ai", 443)),
            ("http://localhost:7860/v1", ("localhost", 7860)),
            ("http://example.com", ("example.com", 80)),
            ("api.tavily.com", ("api.tavily.com", 80)),  # 裸 host
        ],
    )
    def test_parse(self, url: str, want: tuple[str, int]) -> None:
        assert _host_port_from_url(url) == want

    def test_default_port_param(self) -> None:
        assert _host_port_from_url("example.com", default_port=8080) == (
            "example.com",
            8080,
        )


@pytest.mark.unit
class TestProbeToDict:
    def test_ok_minimal(self) -> None:
        p = ProbeResult(ok=True, latency_ms=12.3, checked_at=1700000000.0)
        d = _probe_to_dict(p)
        assert d["ok"] is True
        assert d["latency_ms"] == 12.3
        assert "error" not in d
        assert "reason" not in d

    def test_failure_includes_error_strips_latency(self) -> None:
        p = ProbeResult(ok=False, error="timeout", checked_at=1700000000.0)
        d = _probe_to_dict(p)
        assert d["ok"] is False
        assert d["error"] == "timeout"
        assert "latency_ms" not in d

    def test_na_includes_reason_keeps_ok_null(self) -> None:
        p = ProbeResult(ok=None, reason="no_api_key", checked_at=1700000000.0)
        d = _probe_to_dict(p)
        assert d["ok"] is None
        assert d["reason"] == "no_api_key"


@pytest.mark.unit
class TestDbStatus:
    def test_missing(self, tmp_path: Path) -> None:
        s = Settings(db_path=tmp_path / "nonexistent.db", _env_file=None)  # type: ignore[call-arg]
        d = _db_status(s)
        assert d["ok"] is False
        assert "missing" in d["error"]

    def test_existing(self, tmp_path: Path) -> None:
        db = tmp_path / "foo.db"
        db.write_bytes(b"x" * 1024)
        s = Settings(db_path=db, _env_file=None)  # type: ignore[call-arg]
        d = _db_status(s)
        assert d["ok"] is True
        assert d["size_mb"] == 0.001  # 1KB
        assert d["path"] == str(db)

    def test_expands_tilde(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 把 HOME 指向 tmp，写一个 ~/.foo/bar.db，验证 expand 后能找到
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".foo").mkdir()
        (tmp_path / ".foo" / "bar.db").write_bytes(b"x")
        s = Settings(db_path="~/.foo/bar.db", _env_file=None)  # type: ignore[call-arg]
        d = _db_status(s)
        assert d["ok"] is True
        assert d["path"] == str(tmp_path / ".foo" / "bar.db")


@pytest.mark.unit
@pytest.mark.asyncio
class TestHealthzFull:
    async def test_no_prober_returns_empty_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
        # 清缓存
        from app.api import health as health_mod

        health_mod._cache.clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        out = await healthz_full(s)
        assert out["backend"]["ok"] is True
        assert out["backend"]["port"] == 8769
        assert "uptime_s" in out["backend"]
        assert out["remote"] == {}
        assert out["mic"] == {"ok": "unknown"}

    async def test_with_cached_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
        from app.api import health as health_mod

        health_mod._cache.clear()
        health_mod._cache["heyi_stt_firered"] = ProbeResult(
            ok=True, latency_ms=10.0, checked_at=1700000000.0
        )
        health_mod._cache["yunwu_llm_main"] = ProbeResult(
            ok=None, reason="no_api_key", checked_at=1700000000.0
        )
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        out = await healthz_full(s)
        assert out["remote"]["heyi_stt_firered"]["ok"] is True
        assert out["remote"]["yunwu_llm_main"]["ok"] is None
        assert out["remote"]["yunwu_llm_main"]["reason"] == "no_api_key"


@pytest.mark.unit
@pytest.mark.asyncio
class TestProberLifecycle:
    async def test_start_stop_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # patch _probe_all 避免真打远程，本测试只测生命周期
        from app.api import health as health_mod

        async def fake_probe_all(_s: Settings) -> dict[str, ProbeResult]:
            return {"x": ProbeResult(ok=True, latency_ms=0.1, checked_at=0.0)}

        with patch.object(health_mod, "_probe_all", fake_probe_all):
            await start_prober()
            assert health_mod._prober_task is not None
            # 二次 start 应该是 noop
            existing = health_mod._prober_task
            await start_prober()
            assert health_mod._prober_task is existing

            await stop_prober()
            assert health_mod._prober_task is None
            # 二次 stop 也 noop
            await stop_prober()
            assert health_mod._prober_task is None
