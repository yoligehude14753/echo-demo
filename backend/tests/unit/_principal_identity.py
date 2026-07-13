from __future__ import annotations

from pathlib import Path

import aiosqlite
from app.adapters.repo.connection import configure_aiosqlite_connection
from app.security.models import Principal


async def seed_principal_identity(db_path: Path, *principals: Principal) -> None:
    """Seed server-authored identity parents for lower-level quota unit tests."""

    async with aiosqlite.connect(str(db_path)) as conn:
        await configure_aiosqlite_connection(conn)
        for principal in principals:
            await conn.execute(
                """INSERT OR IGNORE INTO tenants
                   (tenant_id, status, created_at, updated_at)
                   VALUES (?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (principal.tenant_id,),
            )
            await conn.execute(
                """INSERT OR IGNORE INTO users
                   (tenant_id, user_id, status, created_at, updated_at)
                   VALUES (?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (principal.tenant_id, principal.owner_id),
            )
            await conn.execute(
                """INSERT OR IGNORE INTO devices
                   (tenant_id, user_id, device_id, created_at, last_seen_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (principal.tenant_id, principal.owner_id, principal.device_id),
            )
        await conn.commit()


__all__ = ["seed_principal_identity"]
