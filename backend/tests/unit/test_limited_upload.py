from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.models import Principal
from app.upload import UploadTooLarge, read_limited_upload
from fastapi import UploadFile

from tests.unit._principal_identity import seed_principal_identity


class _ChunkedUpload:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        end = len(self._data) if size < 0 else self._offset + size
        chunk = self._data[self._offset : end]
        self._offset += len(chunk)
        return chunk


@pytest.fixture
async def upload_context(tmp_path: Path) -> tuple[PrincipalGovernor, Principal]:
    settings = Settings(
        db_path=tmp_path / "uploads.db",
        quota_upload_bytes_per_day=12,
        quota_storage_bytes=12,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    governor = PrincipalGovernor(
        settings,
        now=lambda: datetime(2026, 7, 11, tzinfo=UTC),
    )
    principal = Principal("tenant", "device", "owner", "session", "public")
    await seed_principal_identity(settings.db_path, principal)
    return governor, principal


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upload_is_read_in_bounded_chunks_and_storage_can_be_released(
    upload_context: tuple[PrincipalGovernor, Principal],
) -> None:
    governor, principal = upload_context
    source = _ChunkedUpload(b"abcdefgh")
    result = await read_limited_upload(
        cast(UploadFile, source),
        max_bytes=10,
        chunk_bytes=3,
        governor=governor,
        principal=principal,
        persistent=True,
    )
    assert result.data == b"abcdefgh"
    assert source.read_sizes == [3, 3, 3, 3]
    assert await governor.usage(principal, "storage_bytes") == 8
    await result.release_storage()
    assert await governor.usage(principal, "storage_bytes") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upload_stops_after_first_byte_over_limit_and_settles_read_chunks(
    upload_context: tuple[PrincipalGovernor, Principal],
) -> None:
    governor, principal = upload_context
    source = _ChunkedUpload(b"0123456789overflow")
    with pytest.raises(UploadTooLarge) as error:
        await read_limited_upload(
            cast(UploadFile, source),
            max_bytes=10,
            chunk_bytes=4,
            governor=governor,
            principal=principal,
        )
    assert error.value.observed_bytes == 11
    assert await governor.usage(principal, "upload_bytes") == 11
    assert all(0 < size <= 4 for size in source.read_sizes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upload_daily_budget_is_enforced_after_bounded_read(
    upload_context: tuple[PrincipalGovernor, Principal],
) -> None:
    governor, principal = upload_context
    first = _ChunkedUpload(b"12345678")
    second = _ChunkedUpload(b"12345")
    await read_limited_upload(
        cast(UploadFile, first),
        max_bytes=10,
        chunk_bytes=4,
        governor=governor,
        principal=principal,
    )
    with pytest.raises(QuotaExceeded, match="upload_bytes"):
        await read_limited_upload(
            cast(UploadFile, second),
            max_bytes=10,
            chunk_bytes=4,
            governor=governor,
            principal=principal,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_upload_reservation_settles_to_actual_file_bytes(
    upload_context: tuple[PrincipalGovernor, Principal],
) -> None:
    governor, principal = upload_context
    reservation = await governor.reserve_upload_bytes(principal, 12)
    result = await read_limited_upload(
        cast(UploadFile, _ChunkedUpload(b"payload")),
        max_bytes=10,
        chunk_bytes=3,
        governor=governor,
        principal=principal,
        upload_reservation=reservation,
    )

    assert result.data == b"payload"
    assert await governor.usage(principal, "upload_bytes") == 7
    await reservation.release()
    assert await governor.usage(principal, "upload_bytes") == 7
