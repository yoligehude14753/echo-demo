"""Ambient WAV quality, quota, retention and owner-isolation boundaries."""

from __future__ import annotations

import os
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.ports.repository import RepositoryPort
from app.schemas.meeting import TranscriptSegment
from app.security.context import bind_principal, reset_principal
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.models import Principal
from app.use_cases.ambient_capture import AmbientCapturePipeline

from tests.unit._principal_identity import seed_principal_identity

VALID_PCM = (12_000).to_bytes(2, "little", signed=True) * 3_200
SILENT_PCM = b"\x00\x00" * 3_200
WAV_BYTES = len(VALID_PCM) + 44


def _principal(name: str) -> Principal:
    return Principal(
        tenant_id=f"tenant-{name}",
        device_id=f"device-{name}",
        owner_id=f"owner-{name}",
        session_id=f"session-{name}",
        mode="public",
    )


def _settings(
    tmp_path: Path,
    *,
    rms_gate: int = 100,
    retention_s: float = 86_400,
    owner_max_bytes: int = 1024 * 1024,
    quota_storage_bytes: int = 1024 * 1024,
) -> Settings:
    return Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=rms_gate,
        ambient_frame_rms_threshold=100,
        ambient_min_speech_frame_ratio=0.1,
        ambient_min_stt_chars=0,
        ambient_max_cps=1000,
        ambient_llm_punctuate=False,
        ambient_audio_retention_s=retention_s,
        ambient_audio_owner_max_bytes=owner_max_bytes,
        quota_storage_bytes=quota_storage_bytes,
        _env_file=None,  # type: ignore[call-arg]
    )


def _pipeline(
    settings: Settings,
    *,
    repository: RepositoryPort | None = None,
    governor: PrincipalGovernor | None = None,
    principal: Principal | None = None,
) -> AmbientCapturePipeline:
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[
            TranscriptSegment(text="这是一段有效语音", start_ms=0, end_ms=200),
        ]
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-test")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])
    return AmbientCapturePipeline(
        settings=settings,
        stt=stt,
        rag=rag,
        meeting=meeting,
        repository=repository,
        governor=governor,
        principal=principal,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_silence_is_gated_before_any_wav_or_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, rms_gate=800)
    pipeline = _pipeline(settings)

    result = await pipeline.ingest_chunk(SILENT_PCM)

    assert result.stt_status == "gated"
    assert result.audio_ref == ""
    assert not Path(settings.storage_dir).exists()
    assert pipeline.get_stats().audio_files_stored == 0
    pipeline._stt.transcribe.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_storage_quota_rejects_without_file_or_ledger_residue(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, quota_storage_bytes=WAV_BYTES - 1)
    principal = _principal("quota")
    assert (await run_migrations(settings.db_path)).errors == []
    await seed_principal_identity(settings.db_path, principal)
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    governor = PrincipalGovernor(settings)
    token = bind_principal(principal)
    try:
        pipeline = _pipeline(
            settings,
            repository=repo,
            governor=governor,
            principal=principal,
        )
        with pytest.raises(QuotaExceeded, match="storage_bytes"):
            await pipeline.ingest_chunk(VALID_PCM)

        assert not list(Path(settings.storage_dir).rglob("*.wav"))
        assert not list(Path(settings.storage_dir).rglob("*.tmp"))
        assert await repo.list_ambient_audio_files() == []
        assert await governor.usage(principal, "storage_bytes") == 0
        assert await governor.usage(principal, "upload_bytes") == 0
        assert pipeline.get_stats().audio_quota_rejected == 1
        pipeline._stt.transcribe.assert_not_awaited()  # type: ignore[attr-defined]
    finally:
        reset_principal(token)
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_audio_gc_deletes_file_and_detaches_segment_ref(tmp_path: Path) -> None:
    settings = _settings(tmp_path, retention_s=1.0)
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    try:
        pipeline = _pipeline(settings, repository=repo)
        first = await pipeline.ingest_chunk(VALID_PCM)
        first_path = Path(first.audio_ref)
        old = time.time() - 10
        os.utime(first_path, (old, old))

        second = await pipeline.ingest_chunk(VALID_PCM)

        assert not first_path.exists()
        assert Path(second.audio_ref).exists()
        inventory = await repo.list_ambient_audio_files()
        assert [row.audio_ref for row in inventory] == [second.audio_ref]
        segments = await repo.list_ambient_segments(limit=10)
        assert {row.audio_ref for row in segments} == {"", second.audio_ref}
        assert pipeline.get_stats().audio_files_deleted == 1
        assert pipeline.get_stats().audio_bytes_deleted == WAV_BYTES
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_expired_gc_releases_exact_storage_charge(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        retention_s=1.0,
        quota_storage_bytes=WAV_BYTES * 2,
    )
    principal = _principal("release")
    assert (await run_migrations(settings.db_path)).errors == []
    await seed_principal_identity(settings.db_path, principal)
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    governor = PrincipalGovernor(settings)
    token = bind_principal(principal)
    try:
        pipeline = _pipeline(
            settings,
            repository=repo,
            governor=governor,
            principal=principal,
        )
        first = await pipeline.ingest_chunk(VALID_PCM)
        old = time.time() - 10
        os.utime(first.audio_ref, (old, old))
        assert await governor.usage(principal, "storage_bytes") == WAV_BYTES

        second = await pipeline.ingest_chunk(VALID_PCM)

        assert not Path(first.audio_ref).exists()
        assert Path(second.audio_ref).exists()
        assert await governor.usage(principal, "storage_bytes") == WAV_BYTES
        assert pipeline.get_stats().audio_bytes_deleted == WAV_BYTES
    finally:
        reset_principal(token)
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_capacity_gc_evicts_oldest_file_before_new_write(tmp_path: Path) -> None:
    settings = _settings(tmp_path, owner_max_bytes=WAV_BYTES + 100)
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    try:
        pipeline = _pipeline(settings, repository=repo)
        first = await pipeline.ingest_chunk(VALID_PCM)
        first_path = Path(first.audio_ref)
        old = time.time() - 10
        os.utime(first_path, (old, old))

        second = await pipeline.ingest_chunk(VALID_PCM)

        assert not first_path.exists()
        assert Path(second.audio_ref).exists()
        assert list(Path(settings.storage_dir).rglob("*.wav")) == [Path(second.audio_ref)]
        assert pipeline.get_stats().audio_files_deleted == 1
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gc_never_crosses_owner_scope(tmp_path: Path) -> None:
    settings = _settings(tmp_path, owner_max_bytes=WAV_BYTES + 100)
    owner_a = _principal("a")
    owner_b = _principal("b")
    pipeline_a = _pipeline(settings, principal=owner_a)
    pipeline_b = _pipeline(settings, principal=owner_b)

    file_a = Path((await pipeline_a.ingest_chunk(VALID_PCM)).audio_ref)
    first_b = Path((await pipeline_b.ingest_chunk(VALID_PCM)).audio_ref)
    old = time.time() - 10
    os.utime(first_b, (old, old))
    second_b = Path((await pipeline_b.ingest_chunk(VALID_PCM)).audio_ref)

    assert file_a.exists()
    assert not first_b.exists()
    assert second_b.exists()
    assert file_a.parents[1] != second_b.parents[1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_public_chunk_keeps_wav_registry_segment_and_quota_in_sync(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    principal = _principal("valid")
    assert (await run_migrations(settings.db_path)).errors == []
    await seed_principal_identity(settings.db_path, principal)
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    governor = PrincipalGovernor(settings)
    token = bind_principal(principal)
    try:
        pipeline = _pipeline(
            settings,
            repository=repo,
            governor=governor,
            principal=principal,
        )
        result = await pipeline.ingest_chunk(VALID_PCM)

        saved = Path(result.audio_ref)
        assert result.ambient_stored is True
        assert result.stt_status == "ok"
        assert saved.exists() and saved.stat().st_size == WAV_BYTES
        with wave.open(str(saved), "rb") as wav:
            assert wav.getframerate() == 16_000
            assert wav.getnchannels() == 1
            assert wav.getnframes() == len(VALID_PCM) // 2
        inventory = await repo.list_ambient_audio_files()
        assert len(inventory) == 1
        assert inventory[0].audio_ref == result.audio_ref
        assert inventory[0].size_bytes == saved.stat().st_size
        assert inventory[0].quota_charged is True
        segments = await repo.list_ambient_segments(limit=10)
        assert len(segments) == 1 and segments[0].audio_ref == result.audio_ref
        assert await governor.usage(principal, "storage_bytes") == saved.stat().st_size
        assert await governor.usage(principal, "upload_bytes") == 0
        stats = pipeline.get_stats()
        assert stats.audio_files_stored == 1
        assert stats.audio_bytes_stored == saved.stat().st_size
        assert stats.last_audio_stored_at is not None
    finally:
        reset_principal(token)
        await repo.aclose()
