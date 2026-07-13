from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.models import Principal

from tests.unit._principal_identity import seed_principal_identity


def _principal(name: str) -> Principal:
    return Principal(
        tenant_id=f"tenant-{name}",
        device_id=f"device-{name}",
        owner_id=f"owner-{name}",
        session_id=f"session-{name}",
        mode="public",
    )


@pytest.fixture
async def governor(tmp_path: Path) -> PrincipalGovernor:
    settings = Settings(
        db_path=tmp_path / "quota.db",
        quota_requests_per_minute=2,
        quota_concurrent_requests=1,
        quota_concurrent_expensive_tasks=1,
        quota_websocket_connections=1,
        quota_upload_bytes_per_day=10,
        quota_storage_bytes=8,
        quota_llm_tokens_per_day=20,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    await seed_principal_identity(
        settings.db_path,
        *(_principal(name) for name in ("a", "b", "leases", "ledger")),
    )
    return PrincipalGovernor(
        settings,
        now=lambda: datetime(2026, 7, 11, 12, 34, 5, tzinfo=UTC),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_budget_is_durable_and_principal_scoped(
    governor: PrincipalGovernor,
) -> None:
    first = _principal("a")
    second = _principal("b")
    async with governor.request(first, method="GET", path="/meetings"):
        pass
    async with governor.request(first, method="GET", path="/meetings"):
        pass
    with pytest.raises(QuotaExceeded, match="requests"):
        async with governor.request(first, method="GET", path="/meetings"):
            pass

    restarted = PrincipalGovernor(governor.settings, now=governor._now)
    with pytest.raises(QuotaExceeded, match="requests"):
        async with restarted.request(first, method="GET", path="/meetings"):
            pass
    async with restarted.request(second, method="GET", path="/meetings"):
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_expensive_and_websocket_leases_release(
    governor: PrincipalGovernor,
) -> None:
    principal = _principal("leases")
    request = governor.request(principal, method="POST", path="/capture/chunk")
    await request.__aenter__()
    try:
        with pytest.raises(QuotaExceeded, match="requests"):
            async with governor.request(principal, method="POST", path="/capture/chunk"):
                pass
    finally:
        await request.__aexit__(None, None, None)

    ws = await governor.websocket(principal)
    with pytest.raises(QuotaExceeded, match="websockets"):
        await governor.websocket(principal)
    ws.release()
    (await governor.websocket(principal)).release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upload_storage_and_llm_ledgers_reserve_and_settle(
    governor: PrincipalGovernor,
) -> None:
    principal = _principal("ledger")
    storage = await governor.reserve_upload(principal, 6, persistent=True)
    assert storage is not None
    assert await governor.usage(principal, "upload_bytes") == 6
    assert await governor.usage(principal, "storage_bytes") == 6

    with pytest.raises(QuotaExceeded, match="storage_bytes"):
        await governor.reserve_upload(principal, 3, persistent=True)
    assert await governor.usage(principal, "upload_bytes") == 9
    await storage.release()
    assert await governor.usage(principal, "storage_bytes") == 0

    tokens = await governor.reserve_llm_tokens(principal, 18)
    await tokens.settle(7)
    assert await governor.usage(principal, "llm_tokens") == 7
    with pytest.raises(QuotaExceeded, match="llm_tokens"):
        await governor.reserve_llm_tokens(principal, 14)
