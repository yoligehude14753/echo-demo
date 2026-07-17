"""Persistent per-user multi-device capture selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.adapters.repo.connection import open_aiosqlite_connection

CaptureMode = Literal["single", "multi"]


@dataclass(frozen=True, slots=True)
class CaptureSelection:
    mode: CaptureMode
    selected_device_ids: tuple[str, ...]
    revision: int

    def allows(self, device_id: str) -> bool:
        return device_id in self.selected_device_ids

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "selectedDeviceIds": list(self.selected_device_ids),
            "revision": self.revision,
        }


class CaptureSelectionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    async def get(self, tenant_id: str, owner_id: str) -> CaptureSelection:
        async with open_aiosqlite_connection(self._db_path) as conn:
            cursor = await conn.execute(
                """SELECT mode, selected_device_ids_json, revision
                   FROM capture_selections WHERE tenant_id = ? AND owner_id = ?""",
                (tenant_id, owner_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return CaptureSelection("single", (), 0)
        values = json.loads(str(row[1]))
        return CaptureSelection(str(row[0]), tuple(str(value) for value in values), int(row[2]))  # type: ignore[arg-type]

    async def update(
        self,
        tenant_id: str,
        owner_id: str,
        *,
        mode: CaptureMode,
        selected_device_ids: list[str],
        expected_revision: int,
    ) -> CaptureSelection:
        normalized = tuple(dict.fromkeys(value.strip() for value in selected_device_ids if value.strip()))
        if mode == "single" and len(normalized) != 1:
            raise ValueError("single mode requires exactly one selected device")
        if mode == "multi" and not normalized:
            raise ValueError("multi mode requires at least one selected device")
        async with open_aiosqlite_connection(self._db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT revision FROM capture_selections WHERE tenant_id = ? AND owner_id = ?",
                (tenant_id, owner_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            current_revision = int(row[0]) if row is not None else 0
            if expected_revision != current_revision:
                await conn.rollback()
                raise RuntimeError("capture selection revision conflict")
            revision = current_revision + 1
            await conn.execute(
                """INSERT INTO capture_selections
                       (tenant_id, owner_id, mode, selected_device_ids_json, revision)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id) DO UPDATE SET
                       mode = excluded.mode,
                       selected_device_ids_json = excluded.selected_device_ids_json,
                       revision = excluded.revision""",
                (tenant_id, owner_id, mode, json.dumps(normalized), revision),
            )
            await conn.commit()
        return CaptureSelection(mode, normalized, revision)
