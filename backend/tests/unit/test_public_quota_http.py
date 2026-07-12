from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import Settings
from app.main import _guard_sse_body, create_app
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.security.models import Principal
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_public_http_request_quota_is_enforced_per_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "public-quota.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        quota_requests_per_minute=2,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    deps_mod.reset_deps_for_test()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings

    with TestClient(app) as client:
        session_a_response = client.post(
            "/session/enroll",
            json={
                "enrollment_id": "quota-a-" + "e" * 40,
                "device_secret": "quota-a-" + "s" * 40,
            },
        )
        session_b_response = client.post(
            "/session/enroll",
            json={
                "enrollment_id": "quota-b-" + "e" * 40,
                "device_secret": "quota-b-" + "s" * 40,
            },
        )
        assert session_a_response.status_code == session_b_response.status_code == 201
        session_a = session_a_response.json()
        session_b = session_b_response.json()
        headers_a = {"Authorization": f"Bearer {session_a['token']}"}
        headers_b = {"Authorization": f"Bearer {session_b['token']}"}

        assert client.get("/meetings", headers=headers_a).status_code == 200
        assert client.get("/meetings", headers=headers_a).status_code == 200
        blocked = client.get("/meetings", headers=headers_a)
        assert blocked.status_code == 429
        assert blocked.json()["error"] == {
            "code": "quota_exceeded",
            "message": "resource quota exceeded",
            "metric": "requests",
            "limit": 2,
            "used": 2,
        }
        assert int(blocked.headers["Retry-After"]) >= 1
        assert client.get("/meetings", headers=headers_b).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("concurrent_requests", "concurrent_expensive", "expected_metric"),
    [(1, 1, "requests"), (2, 1, "expensive_tasks")],
)
async def test_public_stream_holds_request_and_expensive_leases_until_body_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_requests: int,
    concurrent_expensive: int,
    expected_metric: str,
) -> None:
    settings = Settings(
        db_path=tmp_path / "public-stream-quota.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        quota_requests_per_minute=20,
        quota_concurrent_requests=concurrent_requests,
        quota_concurrent_expensive_tasks=concurrent_expensive,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    deps_mod.reset_deps_for_test()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings

    class GatedLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, _messages: list[ChatMessage], **_kwargs: Any) -> LLMResponse:
            self.started.set()
            await self.release.wait()
            return LLMResponse(
                content="ok",
                model="gated",
                usage=LLMUsage(),
                latency_ms=1.0,
            )

        async def chat_stream(
            self,
            _messages: list[ChatMessage],
            **_kwargs: Any,
        ) -> AsyncIterator[str]:
            yield "unused"

    llm = GatedLLM()
    app.dependency_overrides[deps_mod.get_llm_singleton] = lambda: llm
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        enrolled = await client.post(
            "/session/enroll",
            json={
                "enrollment_id": "stream-quota-" + "e" * 40,
                "device_secret": "stream-quota-" + "s" * 40,
            },
        )
        assert enrolled.status_code == 201
        headers = {"Authorization": f"Bearer {enrolled.json()['token']}"}

        first = asyncio.create_task(
            client.post("/chat", headers=headers, json={"question": "first"})
        )
        await asyncio.wait_for(llm.started.wait(), timeout=2)
        blocked = await asyncio.wait_for(
            client.post("/chat", headers=headers, json={"question": "second"}),
            timeout=2,
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["metric"] == expected_metric

        llm.release.set()
        assert (await asyncio.wait_for(first, timeout=2)).status_code == 200
        assert (
            await client.post("/chat", headers=headers, json={"question": "after"})
        ).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_stream_releases_quota_and_runtime_leases() -> None:
    exited = False
    released = False
    flushed = False

    class QuotaContext:
        async def __aexit__(self, *_args: object) -> None:
            nonlocal exited
            exited = True

    class RuntimeLease:
        def release(self) -> None:
            nonlocal released
            released = True

    class RuntimeRegistry:
        async def flush_closures(self) -> None:
            nonlocal flushed
            flushed = True

    async def blocked_body() -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b"unreachable"

    guarded = _guard_sse_body(
        blocked_body(),
        quota_context=QuotaContext(),
        runtime_lease=RuntimeLease(),  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry(),
        principal=Principal("tenant", "device", "owner", "session", "public"),
    )
    pending = asyncio.create_task(anext(guarded))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert exited is True
    assert released is True
    assert flushed is True
