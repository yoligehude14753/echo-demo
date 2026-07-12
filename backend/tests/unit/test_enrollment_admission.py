from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import Settings, get_settings
from app.main import create_app
from app.security.sessions import (
    EnrollmentAdmissionLimitError,
    EnrollmentAdmissionPolicy,
    SessionStore,
)
from fastapi.testclient import TestClient


def _credentials(label: str) -> tuple[str, str]:
    return (
        f"enrollment-{label}-" + "e" * 40,
        f"device-secret-{label}-" + "s" * 40,
    )


def _policy(**overrides: int | float) -> EnrollmentAdmissionPolicy:
    values: dict[str, int | float] = {
        "window_s": 60.0,
        "peer_max_per_window": 100,
        "global_max_per_window": 100,
        "peer_max_per_day": 100,
        "global_max_per_day": 100,
        "total_active_max": 100,
        "cleanup_batch_size": 100,
    }
    values.update(overrides)
    return EnrollmentAdmissionPolicy(**values)  # type: ignore[arg-type]


async def _store(
    tmp_path: Path,
    *,
    policy: EnrollmentAdmissionPolicy,
    credential_ttl: timedelta = timedelta(days=1),
) -> tuple[SessionStore, Path, list[datetime]]:
    db_path = tmp_path / "enrollment-admission.db"
    assert (await run_migrations(db_path)).errors == []
    clock = [datetime(2026, 7, 12, 0, 0, tzinfo=UTC)]
    return (
        SessionStore(
            db_path,
            credential_ttl=credential_ttl,
            admission_policy=policy,
            now=lambda: clock[0],
        ),
        db_path,
        clock,
    )


@pytest.mark.unit
async def test_idempotent_retry_and_renew_do_not_consume_new_identity_budget_after_restart(
    tmp_path: Path,
) -> None:
    policy = _policy(
        peer_max_per_window=1,
        global_max_per_window=1,
        peer_max_per_day=1,
        global_max_per_day=1,
        total_active_max=1,
    )
    first_store, db_path, clock = await _store(tmp_path, policy=policy)
    enrollment_id, device_secret = _credentials("stable")
    first = await first_store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
    )

    restarted = SessionStore(
        db_path,
        admission_policy=policy,
        now=lambda: clock[0],
    )
    retry = await restarted.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
    )
    assert retry.session.principal.owner_id == first.session.principal.owner_id
    renewed = await restarted.renew_public_session(device_secret)
    assert renewed.principal.owner_id == first.session.principal.owner_id

    next_enrollment, next_secret = _credentials("blocked")
    with pytest.raises(EnrollmentAdmissionLimitError) as blocked:
        await restarted.enroll_public_device(
            enrollment_id=next_enrollment,
            device_secret=next_secret,
            peer_key="peer-a",
        )
    assert blocked.value.reason == "peer_window"
    assert blocked.value.retry_after_s >= 1
    async with aiosqlite.connect(str(db_path)) as conn:
        assert await (
            await conn.execute("SELECT COUNT(*) FROM public_enrollment_admissions")
        ).fetchone() == (1,)


@pytest.mark.unit
async def test_two_store_instances_cannot_cross_global_concurrent_boundary(tmp_path: Path) -> None:
    policy = _policy(global_max_per_window=1)
    first, db_path, clock = await _store(tmp_path, policy=policy)
    second = SessionStore(db_path, admission_policy=policy, now=lambda: clock[0])

    async def enroll(store: SessionStore, label: str, peer: str) -> object:
        enrollment_id, device_secret = _credentials(label)
        return await store.enroll_public_device(
            enrollment_id=enrollment_id,
            device_secret=device_secret,
            peer_key=peer,
        )

    results = await asyncio.gather(
        enroll(first, "concurrent-a", "peer-a"),
        enroll(second, "concurrent-b", "peer-b"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, EnrollmentAdmissionLimitError) for result in results) == 1
    rejected = next(
        result for result in results if isinstance(result, EnrollmentAdmissionLimitError)
    )
    assert rejected.reason == "global_window"
    async with aiosqlite.connect(str(db_path)) as conn:
        assert await (
            await conn.execute("SELECT COUNT(*) FROM public_enrollment_admissions")
        ).fetchone() == (1,)
        assert await (
            await conn.execute("SELECT COUNT(*) FROM tenants WHERE tenant_id != 'legacy-local'")
        ).fetchone() == (1,)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "advance_s", "peers", "expected_reason"),
    [
        (_policy(peer_max_per_window=1), 0, ("peer-a", "peer-a"), "peer_window"),
        (_policy(global_max_per_window=1), 0, ("peer-a", "peer-b"), "global_window"),
        (
            _policy(window_s=1, peer_max_per_day=1),
            2,
            ("peer-a", "peer-a"),
            "peer_day",
        ),
        (
            _policy(window_s=1, global_max_per_day=1),
            2,
            ("peer-a", "peer-b"),
            "global_day",
        ),
    ],
)
async def test_peer_and_global_rolling_and_daily_windows_are_durable(
    tmp_path: Path,
    policy: EnrollmentAdmissionPolicy,
    advance_s: int,
    peers: tuple[str, str],
    expected_reason: str,
) -> None:
    store, db_path, clock = await _store(tmp_path, policy=policy)
    first_id, first_secret = _credentials("window-a")
    await store.enroll_public_device(
        enrollment_id=first_id,
        device_secret=first_secret,
        peer_key=peers[0],
    )
    clock[0] += timedelta(seconds=advance_s)

    restarted = SessionStore(db_path, admission_policy=policy, now=lambda: clock[0])
    second_id, second_secret = _credentials("window-b")
    with pytest.raises(EnrollmentAdmissionLimitError) as blocked:
        await restarted.enroll_public_device(
            enrollment_id=second_id,
            device_secret=second_secret,
            peer_key=peers[1],
        )
    assert blocked.value.reason == expected_reason


@pytest.mark.unit
async def test_rolling_window_releases_at_exact_deadline(tmp_path: Path) -> None:
    policy = _policy(window_s=1, peer_max_per_window=1)
    store, db_path, clock = await _store(tmp_path, policy=policy)
    first_id, first_secret = _credentials("deadline-a")
    await store.enroll_public_device(
        enrollment_id=first_id,
        device_secret=first_secret,
        peer_key="peer-a",
    )
    clock[0] += timedelta(seconds=1)

    restarted = SessionStore(db_path, admission_policy=policy, now=lambda: clock[0])
    second_id, second_secret = _credentials("deadline-b")
    admitted = await restarted.enroll_public_device(
        enrollment_id=second_id,
        device_secret=second_secret,
        peer_key="peer-a",
    )
    assert admitted.session.principal.owner_id


@pytest.mark.unit
async def test_total_active_limit_is_independent_from_peer_and_global_windows(
    tmp_path: Path,
) -> None:
    policy = _policy(total_active_max=1)
    store, _db_path, _clock = await _store(tmp_path, policy=policy)
    first_id, first_secret = _credentials("active-a")
    await store.enroll_public_device(
        enrollment_id=first_id,
        device_secret=first_secret,
        peer_key="peer-a",
    )
    second_id, second_secret = _credentials("active-b")
    with pytest.raises(EnrollmentAdmissionLimitError) as blocked:
        await store.enroll_public_device(
            enrollment_id=second_id,
            device_secret=second_secret,
            peer_key="peer-b",
        )
    assert blocked.value.reason == "total_active"


@pytest.mark.unit
async def test_expired_resource_free_identity_chain_is_collected_without_erasing_admission(
    tmp_path: Path,
) -> None:
    policy = _policy(total_active_max=1)
    store, db_path, clock = await _store(
        tmp_path,
        policy=policy,
        credential_ttl=timedelta(seconds=1),
    )
    enrollment_id, device_secret = _credentials("orphan")
    await store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
        ttl=timedelta(seconds=1),
    )
    clock[0] += timedelta(seconds=2)

    assert await store.cleanup_orphaned_enrollments() == 1
    async with aiosqlite.connect(str(db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT
                       (SELECT COUNT(*) FROM public_enrollments),
                       (SELECT COUNT(*) FROM tenants WHERE tenant_id != 'legacy-local'),
                       (SELECT COUNT(*) FROM public_enrollment_admissions)"""
            )
        ).fetchone()
    assert row == (0, 0, 1)


@pytest.mark.unit
async def test_expired_identity_with_meeting_resource_is_never_collected(tmp_path: Path) -> None:
    policy = _policy()
    store, db_path, clock = await _store(
        tmp_path,
        policy=policy,
        credential_ttl=timedelta(seconds=1),
    )
    enrollment_id, device_secret = _credentials("resource")
    identity = await store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
        ttl=timedelta(seconds=1),
    )
    principal = identity.session.principal
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('protected-meeting', 'ended', ?, ?, ?, ?)""",
            (
                clock[0].isoformat(),
                principal.tenant_id,
                principal.device_id,
                principal.owner_id,
            ),
        )
        await conn.commit()
    clock[0] += timedelta(seconds=2)

    assert await store.cleanup_orphaned_enrollments() == 0
    async with aiosqlite.connect(str(db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT
                       (SELECT COUNT(*) FROM public_enrollments),
                       (SELECT COUNT(*) FROM tenants WHERE tenant_id = ?),
                       (SELECT COUNT(*) FROM meetings WHERE id = 'protected-meeting')""",
                (principal.tenant_id,),
            )
        ).fetchone()
    assert row == (1, 1, 1)


@pytest.fixture
def admission_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings(
        db_path=tmp_path / "admission-http.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        enrollment_admission_peer_max_per_window=1,
        enrollment_admission_global_max_per_window=100,
        enrollment_admission_peer_max_per_day=100,
        enrollment_admission_global_max_per_day=100,
        enrollment_admission_total_active_max=100,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, settings
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_enrollment_http_429_contract_uses_transport_peer_and_renew_still_works(
    admission_http_client: tuple[TestClient, Settings],
) -> None:
    client, _settings = admission_http_client
    first_id, first_secret = _credentials("http-a")
    payload = {"enrollment_id": first_id, "device_secret": first_secret}
    first = client.post(
        "/session/enroll",
        json=payload,
        headers={"X-Forwarded-For": "198.51.100.10"},
    )
    retry = client.post(
        "/session/enroll",
        json=payload,
        headers={"X-Forwarded-For": "203.0.113.20"},
    )
    second_id, second_secret = _credentials("http-b")
    blocked = client.post(
        "/session/enroll",
        json={"enrollment_id": second_id, "device_secret": second_secret},
        headers={"X-Forwarded-For": "192.0.2.30"},
    )

    assert first.status_code == retry.status_code == 201
    assert first.json()["principal"]["owner_id"] == retry.json()["principal"]["owner_id"]
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "enrollment_admission_limit_exceeded"}
    assert int(blocked.headers["Retry-After"]) >= 1
    renewed = client.post(
        "/session/renew",
        json={"device_credential": first_secret},
        headers={"X-Forwarded-For": "192.0.2.99"},
    )
    assert renewed.status_code == 200
