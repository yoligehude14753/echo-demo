"""SQLite production telemetry adapter 的专用单元测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.telemetry import (
    DeletionReason,
    HmacPseudonymizer,
    SQLiteTelemetryAdapter,
    TelemetryQuery,
    parse_telemetry_delete_request,
    parse_telemetry_observation,
    parse_telemetry_query,
)
from app.telemetry.cli import main as telemetry_cli

FAKE_SECRET = b"sqlite-production-test-secret-" + b"x" * 32
BASE_TIME = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _pseudonymizer() -> HmacPseudonymizer:
    return HmacPseudonymizer(
        {"v1": FAKE_SECRET},
        current_key_version="v1",
        rotation_period_s=60,
    )


def _adapter(
    path: Path, *, retention_s: int = 3_600, k_threshold: int = 1
) -> SQLiteTelemetryAdapter:
    return SQLiteTelemetryAdapter(
        path,
        _pseudonymizer(),
        retention_s=retention_s,
        k_threshold=k_threshold,
    )


def _observation(
    event_id: str,
    *,
    subject: str = "one",
    tenant: str = "tenant-a",
    occurred_at: datetime = BASE_TIME,
    success: bool = True,
    **overrides: object,
):
    payload: dict[str, object] = {
        "event_id": event_id,
        "identity": {
            "tenant_id": tenant,
            "user_id": f"user-{subject}",
            "device_id": f"device-{subject}",
        },
        "occurred_at": occurred_at,
        "success": success,
        "operation": "request",
        "platform": "desktop",
        "app_version": "0.3.2",
        "provider": "local",
    }
    payload.update(overrides)
    return parse_telemetry_observation(payload)


async def _record_many(adapter: SQLiteTelemetryAdapter, observations: list[object]) -> None:
    await asyncio.gather(*(adapter.record(observation) for observation in observations))  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reopen_persistence_and_independent_schema(tmp_path: Path) -> None:
    main_db = tmp_path / "main.sqlite"
    telemetry_db = tmp_path / "telemetry.sqlite"
    with sqlite3.connect(main_db) as connection:
        connection.execute("CREATE TABLE main_marker (value TEXT NOT NULL)")

    adapter = _adapter(telemetry_db)
    await adapter.record(_observation("evt-reopen", subject="persisted"))
    reopened = SQLiteTelemetryAdapter(telemetry_db, retention_s=3_600, k_threshold=1)

    assert reopened.stored_event_count == 1
    assert reopened.schema_version == 1
    assert await reopened.query(parse_telemetry_query({"k_threshold": 1}))

    with sqlite3.connect(telemetry_db) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(telemetry_events)")}
    assert "telemetry_schema_version" in tables
    assert "telemetry_events" in tables
    assert "schema_version" not in tables
    assert {
        "tenant_id",
        "user_id",
        "device_id",
        "transcript",
        "raw_audio",
        "error_body",
        "api_key",
    }.isdisjoint(columns)
    with sqlite3.connect(main_db) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'telemetry_%'"
            ).fetchall()
            == []
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_is_idempotent_and_conflict_rolls_back(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite")
    original = _observation("evt-duplicate", subject="same")
    await adapter.record(original)
    await adapter.record(original)
    assert adapter.stored_event_count == 1

    conflicting = _observation(
        "evt-duplicate",
        subject="same",
        success=False,
        failure_reason="timeout",
    )
    with pytest.raises(ValueError, match="event_id"):
        await adapter.record(conflicting)
    assert adapter.stored_event_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlite_failure_rolls_back_the_append(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite")
    with sqlite3.connect(adapter.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_test_event
            BEFORE INSERT ON telemetry_events
            WHEN NEW.app_version = '0.3.99'
            BEGIN
                SELECT RAISE(ABORT, 'forced telemetry failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced telemetry failure"):
        await adapter.record(_observation("evt-rollback", app_version="0.3.99"))
    assert adapter.stored_event_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_append_is_safe(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite")
    observations = [
        _observation(f"evt-concurrent-{index}", subject=f"user-{index}") for index in range(20)
    ]
    await _record_many(adapter, observations)
    assert adapter.stored_event_count == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregate_math_and_dimension_key(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite", k_threshold=1)
    observations = [
        _observation(
            "evt-math-1",
            subject="one",
            end_to_end_latency_ms=100,
            queue_wait_ms=10,
            audio_duration_ms=1_000,
        ),
        _observation(
            "evt-math-2",
            subject="two",
            success=False,
            failure_reason="timeout",
            end_to_end_latency_ms=200,
            queue_wait_ms=20,
        ),
        _observation(
            "evt-math-3",
            subject="three",
            success=False,
            failure_reason="internal",
            end_to_end_latency_ms=300,
            queue_wait_ms=30,
            audio_duration_ms=500,
        ),
        _observation(
            "evt-math-4",
            subject="four",
            end_to_end_latency_ms=400,
            queue_wait_ms=40,
            audio_duration_ms=0,
        ),
        _observation(
            "evt-math-5",
            subject="five",
            platform="android",
            app_version="0.3.3",
            provider="stt",
            end_to_end_latency_ms=50,
        ),
    ]
    await _record_many(adapter, observations)

    aggregates = await adapter.query(parse_telemetry_query({"k_threshold": 1}))
    assert len(aggregates) == 2
    desktop = next(aggregate for aggregate in aggregates if aggregate.platform.value == "desktop")
    assert desktop.distinct_user_count == 4
    assert desktop.request_count == 4
    assert desktop.success_count == 2
    assert desktop.failure_count == 2
    assert desktop.success_rate == 0.5
    assert desktop.latency_sum_ms == 1_000
    assert desktop.queue_wait_sum_ms == 100
    assert desktop.audio_duration_sum_ms == 1_500
    assert desktop.audio_duration_event_count == 3
    assert [(item.reason.value, item.event_count) for item in desktop.failure_reason_counts] == [
        ("internal", 1),
        ("timeout", 1),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_distinct_user_k_suppression_uses_effective_threshold(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite", k_threshold=3)
    await _record_many(
        adapter,
        [_observation(f"evt-k-{index}", subject=f"user-{index}") for index in range(2)],
    )
    assert await adapter.query(parse_telemetry_query({"k_threshold": 1})) == ()

    await adapter.record(_observation("evt-k-2", subject="user-2"))
    visible = await adapter.query(TelemetryQuery(k_threshold=3))
    assert len(visible) == 1
    assert visible[0].distinct_user_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retention_purge_removes_only_expired_events(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite", retention_s=60)
    await adapter.record(
        _observation("evt-old", subject="old", occurred_at=BASE_TIME - timedelta(seconds=61))
    )
    await adapter.record(_observation("evt-new", subject="new", occurred_at=BASE_TIME))

    assert await adapter.purge_expired(now=BASE_TIME) == 1
    assert adapter.stored_event_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_delete_returns_safe_receipt_and_removes_scope(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "telemetry.sqlite")
    await _record_many(
        adapter,
        [
            _observation("evt-delete-one", subject="one", tenant="tenant-delete-raw"),
            _observation("evt-delete-two", subject="two", tenant="tenant-delete-raw"),
            _observation("evt-keep", subject="keep", tenant="tenant-keep"),
        ],
    )
    tenant_pseudonym = (
        _pseudonymizer()
        .materialize(_observation("evt-any", subject="any", tenant="tenant-delete-raw"))
        .identity.tenant_pseudonym
    )

    receipt = await adapter.delete(
        parse_telemetry_delete_request(
            {
                "tenant_pseudonym": tenant_pseudonym,
                "reason": DeletionReason.USER_REQUEST,
            }
        )
    )
    assert receipt.deleted_event_count == 2
    assert adapter.stored_event_count == 1
    assert adapter.deletion_audit == (receipt,)
    assert "tenant-delete-raw" not in receipt.model_dump_json()
    with sqlite3.connect(adapter.db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(telemetry_deletion_audit)")
        }
    assert columns == {"audit_id", "deleted_event_count", "deleted_at", "reason"}


@pytest.mark.unit
def test_cli_outputs_aggregate_json_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "telemetry.sqlite"
    adapter = _adapter(db_path)
    asyncio.run(
        _record_many(
            adapter,
            [_observation(f"evt-cli-{index}", subject=f"cli-{index}") for index in range(5)],
        )
    )

    assert telemetry_cli(["--db", str(db_path), "--k-threshold", "5", "query"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"aggregates"}
    assert len(output["aggregates"]) == 1
    serialized = json.dumps(output)
    assert "event_id" not in serialized
    assert "user-cli-0" not in serialized
    assert "raw_audio" not in serialized


@pytest.mark.unit
def test_cli_supports_purge_and_delete_without_event_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "telemetry.sqlite"
    adapter = _adapter(db_path, retention_s=60)
    asyncio.run(
        adapter.record(_observation("evt-cli-delete", subject="cli-delete", occurred_at=BASE_TIME))
    )
    tenant_pseudonym = (
        _pseudonymizer()
        .materialize(_observation("evt-cli-any", subject="any"))
        .identity.tenant_pseudonym
    )

    assert (
        telemetry_cli(
            ["--db", str(db_path), "purge", "--now", (BASE_TIME + timedelta(seconds=1)).isoformat()]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"purged_event_count": 0}
    assert (
        telemetry_cli(
            [
                "--db",
                str(db_path),
                "delete",
                "--tenant-pseudonym",
                tenant_pseudonym,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["deletion_receipt"]["deleted_event_count"] == 1
    assert "tenant_pseudonym" not in output["deletion_receipt"]
