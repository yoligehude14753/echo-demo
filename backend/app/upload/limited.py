"""分块读取且带 principal 配额的 multipart 上传边界。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import UploadFile

from app.security.governor import PrincipalGovernor, QuotaReservation
from app.security.models import Principal


class UploadTooLarge(ValueError):
    def __init__(self, *, max_bytes: int, observed_bytes: int) -> None:
        super().__init__(f"upload exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes
        self.observed_bytes = observed_bytes


@dataclass(slots=True)
class LimitedUpload:
    data: bytes
    size_bytes: int
    storage_reservation: QuotaReservation | None = None

    async def release_storage(self) -> None:
        if self.storage_reservation is not None:
            await self.storage_reservation.release()
            self.storage_reservation = None


async def read_limited_upload(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_bytes: int,
    governor: PrincipalGovernor,
    principal: Principal,
    persistent: bool = False,
    upload_reservation: QuotaReservation | None = None,
) -> LimitedUpload:
    """Never call unbounded ``UploadFile.read()`` and account accepted bytes."""

    if max_bytes < 1 or chunk_bytes < 1:
        raise ValueError("upload bounds must be positive")
    parts = bytearray()
    try:
        while True:
            remaining = max_bytes - len(parts)
            chunk = await upload.read(min(chunk_bytes, remaining + 1))
            if not chunk:
                break
            if upload_reservation is None:
                await governor.charge_upload_bytes(principal, len(chunk))
            parts.extend(chunk)
            if len(parts) > max_bytes:
                raise UploadTooLarge(max_bytes=max_bytes, observed_bytes=len(parts))
        if upload_reservation is not None:
            await upload_reservation.settle(len(parts))
    except Exception:
        if upload_reservation is not None:
            await upload_reservation.settle(len(parts))
        raise
    data = bytes(parts)
    storage = await governor.reserve_storage(principal, len(data)) if persistent else None
    return LimitedUpload(data=data, size_bytes=len(data), storage_reservation=storage)


__all__ = ["LimitedUpload", "UploadTooLarge", "read_limited_upload"]
