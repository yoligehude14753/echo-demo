import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from loguru import logger

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _db_path() -> Path:
    """Resolve DB path at call-time so tests can override DB_PATH via env."""
    from app.config import get_config
    return Path(get_config().DB_PATH)


@asynccontextmanager
async def get_db():
    """Async context manager yielding a configured aiosqlite connection."""
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


async def _apply_migrations(conn) -> None:
    """
    Apply additive schema migrations for existing databases.
    Each migration is idempotent — safe to run multiple times.
    """
    # v2: add device_id to memory_nodes and memory_edges
    for stmt in [
        "ALTER TABLE memory_nodes ADD COLUMN device_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE memory_edges ADD COLUMN device_id TEXT NOT NULL DEFAULT 'default'",
        "CREATE INDEX IF NOT EXISTS idx_nodes_device ON memory_nodes(device_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_device ON memory_edges(device_id)",
        # v3: ambient_transcripts table (CREATE IF NOT EXISTS is idempotent)
        """CREATE TABLE IF NOT EXISTS ambient_transcripts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id       TEXT NOT NULL,
            text            TEXT NOT NULL,
            speaker_label   TEXT,
            speaker_uuid    TEXT,
            confidence      REAL DEFAULT 0.0,
            source          TEXT NOT NULL DEFAULT 'device',
            router_action   TEXT,
            recorded_at     TEXT NOT NULL,
            processed_for_memory INTEGER NOT NULL DEFAULT 0,
            priority        INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_at_device    ON ambient_transcripts(device_id, recorded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_at_pending   ON ambient_transcripts(processed_for_memory, priority DESC, recorded_at)",
        "CREATE INDEX IF NOT EXISTS idx_at_speaker   ON ambient_transcripts(speaker_uuid)",
        "CREATE INDEX IF NOT EXISTS idx_at_action    ON ambient_transcripts(router_action)",
        "CREATE INDEX IF NOT EXISTS idx_at_priority  ON ambient_transcripts(device_id, priority DESC, processed_for_memory)",
        # v4: priority column for existing databases
        "ALTER TABLE ambient_transcripts ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
        # Backfill: personal rows get priority=2, others stay 0
        "UPDATE ambient_transcripts SET priority=2 WHERE router_action='personal' AND priority=0",
        # v4: source_confidence on memory_nodes for quality-weighted retrieval
        "ALTER TABLE memory_nodes ADD COLUMN source_confidence REAL NOT NULL DEFAULT 0.7",
        "CREATE INDEX IF NOT EXISTS idx_nodes_confidence ON memory_nodes(device_id, source_confidence DESC)",
    ]:
        try:
            await conn.execute(stmt)
        except Exception:
            pass  # column/index already exists


async def init_db() -> None:
    """Apply schema.sql + migrations — idempotent.

    Migration order matters:
      1. _apply_migrations first — adds missing columns to existing tables
         so that subsequent CREATE INDEX statements in schema.sql don't fail
         with 'no such column' on legacy databases.
      2. executescript(schema_sql) — creates missing tables/indexes/views.
    """
    schema_sql = _SCHEMA_PATH.read_text()
    async with get_db() as conn:
        await _apply_migrations(conn)   # add columns first, then indexes
        await conn.executescript(schema_sql)
        await conn.commit()
    logger.info(f"Database initialised at {_db_path().resolve()}")
