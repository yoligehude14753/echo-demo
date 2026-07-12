from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from app.adapters.repo.migrator import run_migrations
from app.api.chat import ChatRequest
from app.api.retrieval import AskRequest
from app.config import Settings
from app.schemas.intent import IntentRequest
from app.security.governor import PrincipalGovernor
from app.security.models import Principal
from app.upload.ingress import (
    UploadIngressCapacityExceeded,
    UploadIngressLimiter,
    UploadIngressMiddleware,
    request_body_limit,
)
from app.upload.ownership import (
    bind_rag_content_doc,
    claim_rag_content,
    rag_blob_path,
    release_rag_content_claim,
    stage_rag_content_blob,
)
from pydantic import ValidationError

from tests.unit._principal_identity import seed_principal_identity


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_ingress_limiter_reserves_count_and_declared_bytes() -> None:
    limiter = UploadIngressLimiter(max_requests=2, max_bytes=10)
    first = await limiter.acquire(8)
    with pytest.raises(UploadIngressCapacityExceeded):
        await limiter.acquire(3)
    second = await limiter.acquire(2)
    with pytest.raises(UploadIngressCapacityExceeded):
        await second.ensure_bytes(3)
    await second.release()
    await first.release()
    recovered = await limiter.acquire(10)
    await recovered.release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_rejects_declared_length_before_downstream() -> None:
    settings = Settings(
        upload_max_file_mb=0.000001,
        upload_multipart_overhead_bytes=64 * 1024,
        public_demo_mode=True,
        _env_file=None,  # type: ignore[call-arg]
    )
    called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal called
        called = True

    middleware = UploadIngressMiddleware(downstream, settings=settings)

    async def receive() -> dict[str, object]:
        raise AssertionError("oversized body must not be read")

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/rag/ingest",
        "headers": [(b"content-length", b"70000")],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_rejects_two_megabyte_json_before_downstream() -> None:
    settings = Settings(
        request_body_max_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal called
        del scope, receive, send
        called = True

    middleware = UploadIngressMiddleware(downstream, settings=settings)

    async def receive() -> dict[str, object]:
        raise AssertionError("oversized JSON body must not be read")

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"content-length", str(2 * 1024 * 1024).encode())],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_counts_chunked_utf8_bytes_without_content_length() -> None:
    settings = Settings(
        request_body_max_bytes=16 * 1024,
        upload_global_inflight_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal called
        del scope, send
        called = True
        while True:
            message = await receive()  # type: ignore[operator]
            if not message.get("more_body", False):
                break

    middleware = UploadIngressMiddleware(downstream, settings=settings)
    utf8_frame = ("你" * 3_000).encode()
    frames = iter(
        [
            {"type": "http.request", "body": utf8_frame, "more_body": True},
            {"type": "http.request", "body": utf8_frame, "more_body": False},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(frames)

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [],
        "state": {},
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert called is True
    assert sent[0]["status"] == 413
    assert scope["state"] == {"upload_body_bytes": 18_000}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_allows_normal_json_and_keeps_upload_ceiling() -> None:
    settings = Settings(
        request_body_max_bytes=16 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    payload = b'{"question":"normal"}'
    observed = bytearray()

    async def downstream(scope: object, receive: object, send: object) -> None:
        del scope
        message = await receive()  # type: ignore[operator]
        observed.extend(message.get("body", b""))
        await send(  # type: ignore[operator]
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware = UploadIngressMiddleware(downstream, settings=settings)
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        assert delivered is False
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"content-length", str(len(payload)).encode())],
        "state": {},
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert bytes(observed) == payload
    assert sent[0]["status"] == 200
    assert request_body_limit(settings, "/chat", "POST") == 16 * 1024
    assert request_body_limit(settings, "/rag/ingest", "POST") > 16 * 1024  # type: ignore[operator]
    assert request_body_limit(settings, "/chat", "GET") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_rejects_lying_length_before_multipart_handler() -> None:
    settings = Settings(
        upload_max_file_mb=0.000001,
        upload_multipart_overhead_bytes=64 * 1024,
        upload_global_inflight_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal called
        called = True
        while True:
            message = await receive()  # type: ignore[operator]
            if not message.get("more_body", False):
                break

    middleware = UploadIngressMiddleware(downstream, settings=settings)
    frames = iter(
        [
            {"type": "http.request", "body": b"x" * 40_000, "more_body": True},
            {"type": "http.request", "body": b"y" * 40_000, "more_body": False},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(frames)

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/capture/chunk",
        "headers": [(b"content-length", b"2")],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert called is True
    assert sent[0]["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_times_out_slow_body_and_releases_capacity() -> None:
    settings = Settings(
        request_body_timeout_s=0.01,
        upload_global_inflight_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    limiter_reached = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        del scope, send
        await receive()  # type: ignore[operator]

    middleware = UploadIngressMiddleware(downstream, settings=settings)
    never = asyncio.Event()

    async def receive() -> dict[str, object]:
        await never.wait()
        return {"type": "http.request", "body": b"x", "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"content-length", b"1")],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 408

    try:
        recovered = await middleware.limiter.acquire(settings.upload_global_inflight_bytes)
    except UploadIngressCapacityExceeded:
        limiter_reached = True
    else:
        await recovered.release()
    assert limiter_reached is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completed_body_deadline_does_not_abort_long_streaming_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        request_body_timeout_s=0.01,
        upload_global_concurrent_requests=1,
        upload_global_inflight_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    middleware: UploadIngressMiddleware

    async def downstream(scope: object, receive: object, send: object) -> None:
        del scope
        body = await receive()  # type: ignore[operator]
        assert body == {
            "type": "http.request",
            "body": b'{"question":"stream"}',
            "more_body": False,
        }
        # The upload lease must be available while the SSE response remains
        # open; otherwise a few long streams exhaust global upload capacity.
        concurrent = await middleware.limiter.acquire(0)
        await concurrent.release()
        await send(  # type: ignore[operator]
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        disconnected = await receive()  # type: ignore[operator]
        assert disconnected["type"] == "http.disconnect"
        await send(  # type: ignore[operator]
            {"type": "http.response.body", "body": b"done", "more_body": False}
        )

    middleware = UploadIngressMiddleware(downstream, settings=settings)
    calls = 0
    wait_for_calls = 0
    real_wait_for = asyncio.wait_for

    async def tracked_wait_for(
        awaitable: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal wait_for_calls
        wait_for_calls += 1
        timeout_s = args[0] if args else kwargs["timeout"]
        return await real_wait_for(awaitable, timeout=float(timeout_s))  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "wait_for", tracked_wait_for)

    async def receive() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b'{"question":"stream"}',
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/rag/ask",
        "headers": [(b"content-length", b"21")],
        "state": {},
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert calls == 2
    assert wait_for_calls == 1
    assert sent == [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"done", "more_body": False},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_guard_cancellation_releases_global_count_and_bytes() -> None:
    settings = Settings(
        request_body_max_bytes=16 * 1024,
        upload_global_concurrent_requests=1,
        upload_global_inflight_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    entered = asyncio.Event()
    never = asyncio.Event()

    async def downstream(scope: object, receive: object, send: object) -> None:
        del scope, send
        entered.set()
        await receive()  # type: ignore[operator]

    middleware = UploadIngressMiddleware(downstream, settings=settings)

    async def receive() -> dict[str, object]:
        await never.wait()
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        del message

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"content-length", b"1")],
    }
    task = asyncio.create_task(middleware(scope, receive, send))  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    recovered = await middleware.limiter.acquire(settings.upload_global_inflight_bytes)
    await recovered.release()


@pytest.mark.unit
def test_primary_text_schemas_bound_unicode_characters() -> None:
    accepted = "你" * 32_000
    rejected = accepted + "好"

    assert ChatRequest(question=accepted).question == accepted
    assert AskRequest(question=accepted).question == accepted
    assert IntentRequest(text=accepted).text == accepted

    with pytest.raises(ValidationError):
        ChatRequest(question=rejected)
    with pytest.raises(ValidationError):
        AskRequest(question=rejected)
    with pytest.raises(ValidationError):
        IntentRequest(text=rejected)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rag_cas_charges_each_owner_once_and_releases_only_its_acl(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "rag-owner.db",
        storage_dir=tmp_path / "storage",
        quota_storage_bytes=20,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    governor = PrincipalGovernor(settings)
    first = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    second = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")
    await seed_principal_identity(settings.db_path, first, second)

    digest = hashlib.sha256(b"shared").hexdigest()
    first_claim = await claim_rag_content(
        settings.db_path,
        first,
        content_hash=digest,
        size_bytes=6,
        workflow_run_id="run-a",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    second_claim = await claim_rag_content(
        settings.db_path,
        second,
        content_hash=digest,
        size_bytes=6,
        workflow_run_id="run-b",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    duplicate_claim = await claim_rag_content(
        settings.db_path,
        first,
        content_hash=digest,
        size_bytes=6,
        workflow_run_id="run-c",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    assert first_claim.created is True
    assert second_claim.created is True
    assert duplicate_claim.created is False
    assert duplicate_claim.workflow_run_id == "run-a"
    assert await governor.usage(first, "storage_bytes") == 6
    assert await governor.usage(second, "storage_bytes") == 6

    first_path = await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        first,
        content_hash=digest,
        workflow_run_id="run-a",
        content=b"shared",
    )
    second_path = await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        second,
        content_hash=digest,
        workflow_run_id="run-b",
        content=b"shared",
    )
    assert first_path == second_path == rag_blob_path(settings.storage_dir, digest)

    await bind_rag_content_doc(
        settings.db_path,
        first,
        content_hash=digest,
        workflow_run_id="run-a",
        doc_id="doc-a",
    )
    await bind_rag_content_doc(
        settings.db_path,
        second,
        content_hash=digest,
        workflow_run_id="run-b",
        doc_id="doc-b",
    )
    released = await release_rag_content_claim(
        settings.db_path,
        settings.storage_dir,
        first,
        doc_id="doc-a",
    )
    assert released.released_bytes == 6
    assert released.remaining_owners == 1
    assert first_path.exists()
    assert await governor.usage(first, "storage_bytes") == 0
    assert await governor.usage(second, "storage_bytes") == 6

    final_release = await release_rag_content_claim(
        settings.db_path,
        settings.storage_dir,
        second,
        doc_id="doc-b",
    )
    assert final_release.remaining_owners == 0
    assert final_release.physical_deleted is True
    assert not first_path.exists()
    assert await governor.usage(second, "storage_bytes") == 0
