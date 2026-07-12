"""SQLite schema migration runner（P2.4）。

设计要点：
- migrations/ 目录下 ``NNN_<slug>.sql`` 形式的纯 SQL 文件，按 NNN（int）排序执行
- 已应用版本登记在 ``schema_version`` 表，二次启动 skip
- 每个版本用 ``BEGIN IMMEDIATE`` 串行化，并在锁内二次检查版本；共享同一 SQLite
  的多进程不会重复执行非幂等 ALTER/rebuild
- 单文件包裹在一个事务里；任一语句失败 ROLLBACK + errors[] 记录，
  后续文件不再执行（避免基于不一致 schema 继续 migrate）
- v030 起登记 migration filename + SHA-256；已应用文件发生改名或内容漂移时
  fail closed，legacy 行的首次回填与 v030 本身在同一事务
- 表重建前后核对显式 index/trigger/view；未知对象会丢失或被 SQLite 重写时
  整版回滚，不静默吞对象
- 不上 alembic / sqlmodel：当前需要的就是几十行可读的 runner

调用方（lifespan startup）::

    from app.adapters.repo.migrator import run_migrations
    result = await run_migrations(settings.db_path)
    if result.errors:
        raise RuntimeError(f"db migrations failed: {result.errors}")
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)

logger = logging.getLogger("echodesk.migrator")

# NNN_<slug>.sql：NNN 必须 3 位以上数字
_MIGRATION_NAME_RE = re.compile(r"^(\d{3,})_[A-Za-z0-9_\-]+\.sql$")

# bootstrap：在跑任何文件前先建好 schema_version 自身（不算 migration）。
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);
"""

_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_BUSY_TIMEOUT_MS = 60_000
_SQL_IDENTIFIER_PATTERN = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+"|`(?:[^`]|``)+`|\[[^\]]+\])'
_SQL_QUALIFIED_NAME_PATTERN = rf"{_SQL_IDENTIFIER_PATTERN}(?:\s*\.\s*{_SQL_IDENTIFIER_PATTERN})?"
_SQL_IDENTIFIER_TOKEN_RE = re.compile(_SQL_IDENTIFIER_PATTERN)
_VIEW_AFFECTING_ALTER_TABLE_RE = re.compile(
    rf"\AALTER\s+TABLE\s+(?P<target>{_SQL_QUALIFIED_NAME_PATTERN})\s+"
    r"(?:RENAME\b|DROP\s+COLUMN\b)",
    re.IGNORECASE,
)
_DROP_TABLE_RE = re.compile(
    rf"\ADROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    rf"(?P<target>{_SQL_QUALIFIED_NAME_PATTERN})",
    re.IGNORECASE,
)
_EXPLICIT_SCHEMA_OBJECT_DROP_RE = re.compile(
    rf"\ADROP\s+(?P<object_type>INDEX|TRIGGER|VIEW)\s+"
    rf"(?:IF\s+EXISTS\s+)?(?P<target>{_SQL_QUALIFIED_NAME_PATTERN})",
    re.IGNORECASE,
)
_LEADING_SQL_TRIVIA_RE = re.compile(
    r"\A(?:[\s\ufeff]+|--[^\r\n]*(?:\r\n?|\n|\Z)|/\*.*?\*/)*",
    re.DOTALL,
)
_TRANSACTION_CONTROL_RE = re.compile(
    r"\A(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)
# Every intentional rebuild-time index/trigger/view replacement belongs here.
# v013 deliberately replaces a redundant explicit unique index with the new
# composite PRIMARY KEY's SQLite auto-index.
_EXPECTED_REBUILD_OBJECT_CHANGES: dict[int, frozenset[tuple[str, str]]] = {
    13: frozenset({("index", "idx_speakers_owner_speaker_unique")}),
}
_CHECKSUM_COLUMNS = frozenset({"migration_name", "content_sha256"})

# v0.2 shipped a short-lived but real migration lineage before the repository
# migration catalog was consolidated.  Those versions exist in user databases
# and therefore remain immutable facts even though 006-009 are no longer files
# in the current catalog and version 005 now has a different filename.  Never
# bind one of these rows to a migration file that did not create its schema.
_PUBLISHED_LEGACY_DESCRIPTIONS: dict[int, str] = {
    5: "005_speaker_label_user_set",
    6: "006_unified_source_and_conversations",
    7: "007_memory_nodes",
    8: "008_user_billing",
    9: "009_seed_plans",
}
_UNBOUND_PUBLISHED_LEGACY_DESCRIPTIONS = {
    5: _PUBLISHED_LEGACY_DESCRIPTIONS[5],
}
_RESTORED_HISTORICAL_VERSIONS = frozenset({6, 7, 8, 9})
_PUBLISHED_LEGACY_CONTROL_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset(
        {
            "id",
            "email",
            "display_name",
            "password_hash",
            "password_salt",
            "plan_id",
            "created_at",
            "last_login_at",
        }
    ),
    "sessions": frozenset({"token_hash", "user_id", "created_at", "expires_at", "revoked"}),
    "api_keys": frozenset(
        {
            "id",
            "user_id",
            "name",
            "key_prefix",
            "key_hash",
            "created_at",
            "last_used_at",
            "revoked",
        }
    ),
    "plans": frozenset(
        {
            "id",
            "name",
            "monthly_stt_sec",
            "monthly_tts_chars",
            "monthly_llm_tokens",
            "price_micros",
            "created_at",
        }
    ),
    "usage_events": frozenset(
        {
            "id",
            "user_id",
            "api_key_id",
            "capability",
            "units",
            "unit_kind",
            "provider",
            "cost_micros",
            "created_at",
        }
    ),
    "user_model_config": frozenset(
        {
            "user_id",
            "service_mode",
            "stt_mode",
            "stt_base_url",
            "stt_api_key",
            "tts_mode",
            "tts_base_url",
            "tts_api_key",
            "llm_mode",
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "updated_at",
        }
    ),
}
_PUBLISHED_LEGACY_DOMAIN_ARCHIVES: dict[str, tuple[str, frozenset[str]]] = {
    "conversations": (
        "legacy_v6_conversations",
        frozenset(
            {
                "id",
                "turn_id",
                "role",
                "text",
                "source",
                "device_id",
                "speaker_id",
                "speaker_label",
                "trigger",
                "created_at",
            }
        ),
    ),
    "memory_nodes": (
        "legacy_v7_memory_nodes",
        frozenset(
            {
                "id",
                "content",
                "kind",
                "source",
                "device_id",
                "speaker_label",
                "salience",
                "hit_count",
                "created_at",
                "last_seen_at",
            }
        ),
    ),
}
_PUBLISHED_SPEAKER_LABEL_SNAPSHOT = "migration_013_published_speaker_labels"


@dataclass
class MigrationResult:
    """run_migrations 的产物。"""

    applied: list[int] = field(default_factory=list)
    """这次实际跑过的版本号，按执行顺序。"""

    current_version: int = 0
    """跑完后 schema_version 里的最大版本（不含失败的）。"""

    skipped: list[int] = field(default_factory=list)
    """启动前已应用、本次跳过的版本号。"""

    not_applicable: list[int] = field(default_factory=list)
    """恢复进 catalog 但当前高水位已越过、因而不得倒序执行的历史版本。"""

    errors: list[str] = field(default_factory=list)
    """失败信息（人类可读），格式 ``"v{N} ({name}): {err}"``。"""

    orphan_quarantined: int = 0
    """历史孤儿关系进入 migration_orphan_quarantine 的记录数。"""


@dataclass(frozen=True)
class _MigrationFile:
    version: int
    name: str  # 如 "001_initial"
    path: Path
    content: bytes = field(repr=False)
    content_sha256: str

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def sql(self) -> str:
        return self.content.decode("utf-8")


@dataclass(frozen=True)
class _SchemaDependent:
    object_type: str
    name: str
    table_name: str
    sql: str


class _MigrationIntegrityError(RuntimeError):
    """An applied migration no longer matches the shipped catalog."""


def _discover(migrations_dir: Path) -> list[_MigrationFile]:
    """扫描目录，按 NNN 数字排序（而非字典序：避免 010 排在 002 之前）。"""
    if not migrations_dir.is_dir():
        return []
    out: list[_MigrationFile] = []
    for p in migrations_dir.iterdir():
        if not p.is_file() or p.suffix != ".sql":
            continue
        m = _MIGRATION_NAME_RE.match(p.name)
        if not m:
            logger.warning("migrator: skip unrecognized file %s", p.name)
            continue
        content = p.read_bytes()
        out.append(
            _MigrationFile(
                version=int(m.group(1)),
                name=p.stem,
                path=p,
                content=content,
                content_sha256=sha256(content).hexdigest(),
            )
        )
    out.sort(key=lambda mf: mf.version)
    return out


async def _ensure_bootstrap(conn: aiosqlite.Connection) -> None:
    """建 schema_version 自身（IF NOT EXISTS，幂等）。"""
    await conn.execute(_BOOTSTRAP_SQL)
    await conn.commit()


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT version FROM schema_version")
    rows = await cur.fetchall()
    await cur.close()
    return {int(r[0]) for r in rows}


async def _checksum_columns_present(conn: aiosqlite.Connection) -> bool:
    cur = await conn.execute("PRAGMA table_info(schema_version)")
    rows = await cur.fetchall()
    await cur.close()
    return {str(row[1]) for row in rows} >= _CHECKSUM_COLUMNS


def _integrity_mismatch(
    mf: _MigrationFile,
    *,
    registered_name: object,
    registered_sha256: object,
) -> _MigrationIntegrityError:
    details: list[str] = []
    if registered_name != mf.filename:
        details.append(f"name={registered_name!r}, expected={mf.filename!r}")
    if registered_sha256 != mf.content_sha256:
        details.append(f"sha256={registered_sha256!r}, expected={mf.content_sha256!r}")
    return _MigrationIntegrityError(
        f"v{mf.version} ({mf.filename}) registration mismatch: {', '.join(details)}"
    )


async def _assert_registered_migration_tx(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
) -> None:
    if not await _checksum_columns_present(conn):
        return
    cur = await conn.execute(
        """SELECT migration_name, content_sha256 FROM schema_version
           WHERE version = ?""",
        (mf.version,),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return
    if row[0] != mf.filename or row[1] != mf.content_sha256:
        raise _integrity_mismatch(
            mf,
            registered_name=row[0],
            registered_sha256=row[1],
        )


async def _assert_applied_catalog(
    conn: aiosqlite.Connection,
    files: list[_MigrationFile],
) -> None:
    if not await _checksum_columns_present(conn):
        return
    catalog = {mf.version: mf for mf in files}
    if len(catalog) != len(files):
        raise _MigrationIntegrityError("migration catalog contains duplicate versions")
    cur = await conn.execute(
        """SELECT version, description, migration_name, content_sha256
           FROM schema_version ORDER BY version"""
    )
    rows = await cur.fetchall()
    await cur.close()
    for row in rows:
        version = int(row[0])
        description = str(row[1] or "")
        registered_name = row[2]
        registered_sha256 = row[3]
        mf = catalog.get(version)
        if (
            registered_name is None
            and registered_sha256 is None
            and _UNBOUND_PUBLISHED_LEGACY_DESCRIPTIONS.get(version) == description
        ):
            # Preserve the published registration verbatim.  In particular,
            # v005_speaker_label_user_set must not be relabelled as the current
            # 005_agent_tasks file merely because both use integer version 5.
            continue
        if registered_name is None and registered_sha256 is None and mf is None:
            # Historical experimental versions whose files were already absent
            # when v030 established the trust baseline remain auditable but
            # cannot be retroactively verified.
            continue
        if mf is None:
            raise _MigrationIntegrityError(
                f"v{version} ({registered_name!r}) registered migration file is missing"
            )
        if registered_name != mf.filename or registered_sha256 != mf.content_sha256:
            raise _integrity_mismatch(
                mf,
                registered_name=registered_name,
                registered_sha256=registered_sha256,
            )


async def _insert_schema_version_tx(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
) -> None:
    if await _checksum_columns_present(conn):
        await conn.execute(
            """INSERT INTO schema_version
               (version, description, migration_name, content_sha256)
               VALUES (?, ?, ?, ?)""",
            (mf.version, mf.name, mf.filename, mf.content_sha256),
        )
        return
    await conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (?, ?)",
        (mf.version, mf.name),
    )


async def _backfill_migration_checksums_tx(
    conn: aiosqlite.Connection,
    files: list[_MigrationFile],
) -> None:
    """Atomically bind every locatable applied legacy row to its file."""

    if not await _checksum_columns_present(conn):
        return
    for mf in files:
        cur = await conn.execute(
            """SELECT description, migration_name, content_sha256 FROM schema_version
               WHERE version = ?""",
            (mf.version,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            continue
        description = str(row[0] or "")
        registered_name = row[1]
        registered_sha256 = row[2]
        if _UNBOUND_PUBLISHED_LEGACY_DESCRIPTIONS.get(mf.version) == description:
            if registered_name is not None or registered_sha256 is not None:
                raise _MigrationIntegrityError(
                    f"published legacy v{mf.version} registration was unexpectedly rebound"
                )
            continue
        if registered_name is None and registered_sha256 is None:
            await conn.execute(
                """UPDATE schema_version
                   SET migration_name = ?, content_sha256 = ?
                   WHERE version = ?""",
                (mf.filename, mf.content_sha256, mf.version),
            )
            continue
        if registered_name != mf.filename or registered_sha256 != mf.content_sha256:
            raise _integrity_mismatch(
                mf,
                registered_name=registered_name,
                registered_sha256=registered_sha256,
            )


async def _schema_version_description(
    conn: aiosqlite.Connection,
    version: int,
) -> str | None:
    cur = await conn.execute(
        "SELECT description FROM schema_version WHERE version = ?",
        (version,),
    )
    row = await cur.fetchone()
    await cur.close()
    return str(row[0]) if row is not None and row[0] is not None else None


async def _table_columns(
    conn: aiosqlite.Connection,
    table_name: str,
) -> dict[str, str]:
    cur = await conn.execute(
        "SELECT name, type FROM pragma_table_info(?)",
        (table_name,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return {str(row[0]): str(row[1] or "").upper() for row in rows}


async def _prepare_published_legacy_schema_tx(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
) -> None:
    """Adapt immutable v0.2 schema names inside the owning migration transaction."""

    if mf.version == 13:
        await _prepare_published_v13_tx(conn)
    elif mf.version == 18:
        await _prepare_published_v18_tx(conn)
    elif mf.version == 37:
        await _prepare_v37_tx(conn)


async def _prepare_published_v13_tx(conn: aiosqlite.Connection) -> None:
    if await _schema_version_description(conn, 5) != _PUBLISHED_LEGACY_DESCRIPTIONS[5]:
        return
    columns = await _table_columns(conn, "speakers")
    if columns.get("label_user_set") != "INTEGER":
        raise _MigrationIntegrityError(
            "published legacy speakers.label_user_set must remain INTEGER"
        )
    await conn.execute(
        f"""CREATE TEMP TABLE {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT} (
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                speaker_id TEXT NOT NULL,
                label_user_set INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, owner_id, speaker_id)
            )"""
    )
    await conn.execute(
        f"""INSERT INTO {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT}
            (tenant_id, owner_id, speaker_id, label_user_set)
            SELECT tenant_id, owner_id, speaker_id, label_user_set FROM speakers"""
    )


async def _prepare_v37_tx(conn: aiosqlite.Connection) -> None:
    columns = await _table_columns(conn, "ambient_segments")
    for column_name in ("tenant_id", "device_id", "owner_id"):
        if columns.get(column_name) != "TEXT":
            raise _MigrationIntegrityError(
                f"ambient_segments.{column_name} must be TEXT before v37"
            )
    source_type = columns.get("source")
    if source_type is None:
        await conn.execute(
            "ALTER TABLE ambient_segments ADD COLUMN source TEXT NOT NULL DEFAULT 'local'"
        )
    elif source_type != "TEXT":
        raise _MigrationIntegrityError("ambient_segments.source must be TEXT before v37")
    await conn.execute(
        "UPDATE ambient_segments SET device_id = 'legacy-local' WHERE device_id IS NULL"
    )


async def _prepare_published_v18_tx(conn: aiosqlite.Connection) -> None:
    if await _schema_version_description(conn, 8) != _PUBLISHED_LEGACY_DESCRIPTIONS[8]:
        return

    archives: dict[str, tuple[str, frozenset[str]]] = {
        table_name: (f"legacy_v8_{table_name}", expected_columns)
        for table_name, expected_columns in _PUBLISHED_LEGACY_CONTROL_TABLE_COLUMNS.items()
    }
    if await _schema_version_description(conn, 6) == _PUBLISHED_LEGACY_DESCRIPTIONS[6]:
        table_name = "conversations"
        archives[table_name] = _PUBLISHED_LEGACY_DOMAIN_ARCHIVES[table_name]
    if await _schema_version_description(conn, 7) == _PUBLISHED_LEGACY_DESCRIPTIONS[7]:
        table_name = "memory_nodes"
        archives[table_name] = _PUBLISHED_LEGACY_DOMAIN_ARCHIVES[table_name]

    for table_name, (archive_name, expected_columns) in archives.items():
        columns = await _table_columns(conn, table_name)
        if not columns:
            raise _MigrationIntegrityError(f"published legacy table is missing: {table_name}")
        if not expected_columns.issubset(columns):
            actual = ", ".join(sorted(columns))
            raise _MigrationIntegrityError(
                f"published legacy {table_name} schema is not recognized: {actual}"
            )
        if await _table_columns(conn, archive_name):
            raise _MigrationIntegrityError(
                f"published legacy archive already exists: {archive_name}"
            )

    for table_name, (archive_name, _expected_columns) in archives.items():
        await conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{archive_name}"')
    logger.info("migrator: archived published v6-v8 tables under legacy_v*_*")


async def _finalize_published_legacy_schema_tx(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
) -> None:
    if mf.version != 13:
        return
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = ?",
        (_PUBLISHED_SPEAKER_LABEL_SNAPSHOT,),
    )
    snapshot_exists = await cur.fetchone()
    await cur.close()
    if snapshot_exists is None:
        return

    await conn.execute("ALTER TABLE speakers ADD COLUMN label_user_set INTEGER NOT NULL DEFAULT 0")
    await conn.execute(
        f"""UPDATE speakers AS target
            SET label_user_set = (
                SELECT snapshot.label_user_set
                FROM {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT} AS snapshot
                WHERE snapshot.tenant_id = target.tenant_id
                  AND snapshot.owner_id = target.owner_id
                  AND snapshot.speaker_id = target.speaker_id
            )
            WHERE EXISTS (
                SELECT 1 FROM {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT} AS snapshot
                WHERE snapshot.tenant_id = target.tenant_id
                  AND snapshot.owner_id = target.owner_id
                  AND snapshot.speaker_id = target.speaker_id
            )"""
    )
    cur = await conn.execute(
        f"""SELECT COUNT(*) FROM {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT} AS snapshot
            LEFT JOIN speakers AS target
              ON target.tenant_id = snapshot.tenant_id
             AND target.owner_id = snapshot.owner_id
             AND target.speaker_id = snapshot.speaker_id
            WHERE target.speaker_id IS NULL"""
    )
    missing_row = await cur.fetchone()
    await cur.close()
    if int(missing_row[0] if missing_row else 0) != 0:
        raise _MigrationIntegrityError(
            "published legacy speaker label values were not preserved by v13"
        )
    await conn.execute(f"DROP TABLE {_PUBLISHED_SPEAKER_LABEL_SNAPSHOT}")


async def _published_legacy_statement_already_satisfied(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
    statement: str,
) -> bool:
    """Return true only for a verified duplicate from the published v0.2 lineage."""

    normalized = " ".join(_without_leading_sql_trivia(statement).rstrip(";").split()).lower()
    if mf.version == 37:
        return await _v37_statement_already_satisfied(conn, normalized)
    if mf.version == 12:
        return await _v12_statement_already_satisfied(conn, normalized)
    return False


async def _v37_statement_already_satisfied(
    conn: aiosqlite.Connection,
    normalized: str,
) -> bool:
    expected = "alter table speakers add column label_user_set integer not null default 0"
    if normalized != expected:
        return False
    cur = await conn.execute(
        """SELECT type, \"notnull\", dflt_value FROM pragma_table_info(?)
           WHERE name = ?""",
        ("speakers", "label_user_set"),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return False
    if str(row[0] or "").upper() != "INTEGER" or int(row[1]) != 1 or str(row[2]) != "0":
        raise _MigrationIntegrityError("existing speakers.label_user_set is incompatible with v37")
    return True


async def _v12_statement_already_satisfied(
    conn: aiosqlite.Connection,
    normalized: str,
) -> bool:
    if await _schema_version_description(conn, 6) != _PUBLISHED_LEGACY_DESCRIPTIONS[6]:
        return False
    expected = (
        "alter table ambient_segments add column device_id text not null default 'legacy-local'"
    )
    if normalized != expected:
        return False

    columns = await _table_columns(conn, "ambient_segments")
    column_type = columns.get("device_id")
    if column_type is None:
        return False
    if column_type != "TEXT":
        raise _MigrationIntegrityError(
            "published legacy ambient_segments.device_id must remain TEXT"
        )
    await conn.execute(
        "UPDATE ambient_segments SET device_id = 'legacy-local' WHERE device_id IS NULL"
    )
    logger.info("migrator: accepted published v6 ambient_segments.device_id before v12")
    return True


def _restored_history_not_applicable(
    mf: _MigrationFile,
    applied: set[int],
) -> bool:
    if mf.version not in _RESTORED_HISTORICAL_VERSIONS or mf.version in applied:
        return False
    restored_applied = applied & _RESTORED_HISTORICAL_VERSIONS
    if restored_applied:
        return False
    return any(version > max(_RESTORED_HISTORICAL_VERSIONS) for version in applied)


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Split a SQLite script without splitting trigger bodies or quoted text.

    ``Connection.executescript`` implicitly commits an existing transaction, so
    it cannot be used after acquiring the cross-process ``BEGIN IMMEDIATE``
    migration lock. ``sqlite3.complete_statement`` uses SQLite's own lexical
    rules and keeps ``CREATE TRIGGER ... BEGIN ... END`` together.
    """

    pending: list[str] = []
    for char in sql:
        pending.append(char)
        if char != ";":
            continue
        candidate = "".join(pending)
        if not sqlite3.complete_statement(candidate):
            continue
        if statement := candidate.strip():
            yield statement
        pending.clear()
    if statement := "".join(pending).strip():
        yield statement


def _assert_no_transaction_control(statements: tuple[str, ...]) -> None:
    """Keep migration SQL from committing or replacing the outer transaction.

    Statements have already been split with SQLite's own lexical rules, so a
    ``CREATE TRIGGER ... BEGIN ... END`` body starts with ``CREATE`` and remains
    valid. Only a top-level transaction-control keyword is rejected.
    """

    for statement in statements:
        sql = _LEADING_SQL_TRIVIA_RE.sub("", statement, count=1)
        match = _TRANSACTION_CONTROL_RE.match(sql)
        if match is not None:
            raise RuntimeError(
                f"migration SQL must not control transactions: {match.group(0).upper()}"
            )


def _without_leading_sql_trivia(statement: str) -> str:
    return _LEADING_SQL_TRIVIA_RE.sub("", statement, count=1)


def _top_level_sql_statements(sql: str) -> Iterator[str]:
    for statement in _iter_sql_statements(sql):
        if statement := _without_leading_sql_trivia(statement):
            yield statement


def _unqualified_sql_name(target: str) -> str:
    tokens = tuple(match.group(0) for match in _SQL_IDENTIFIER_TOKEN_RE.finditer(target))
    if not tokens:  # pragma: no cover - target already matched the qualified-name grammar
        raise ValueError("SQL object name is missing")
    name = tokens[-1]
    if name.startswith('"'):
        return name[1:-1].replace('""', '"')
    if name.startswith("`"):
        return name[1:-1].replace("``", "`")
    if name.startswith("["):
        return name[1:-1]
    return name


def _allowed_rebuild_changes(
    mf: _MigrationFile,
    sql: str,
) -> frozenset[tuple[str, str]]:
    changes = set(_EXPECTED_REBUILD_OBJECT_CHANGES.get(mf.version, frozenset()))
    for statement in _top_level_sql_statements(sql):
        match = _EXPLICIT_SCHEMA_OBJECT_DROP_RE.match(statement)
        if match is not None:
            changes.add(
                (
                    match.group("object_type").lower(),
                    _unqualified_sql_name(match.group("target")),
                )
            )
    return frozenset(changes)


def _affected_table_names(sql: str) -> tuple[str, ...]:
    names: dict[str, None] = {}
    for statement in _top_level_sql_statements(sql):
        for pattern in (_VIEW_AFFECTING_ALTER_TABLE_RE, _DROP_TABLE_RE):
            match = pattern.match(statement)
            if match is not None:
                names.setdefault(_unqualified_sql_name(match.group("target")), None)
                break
    return tuple(names)


def _view_references_rebuilt_table(view_sql: str, migration_sql: str) -> bool:
    for table_name in _affected_table_names(migration_sql):
        escaped = re.escape(table_name)
        if re.search(
            rf"(?<![A-Za-z0-9_])[`\"\[]?{escaped}[`\"\]]?(?![A-Za-z0-9_])",
            view_sql,
            re.IGNORECASE,
        ):
            return True
    return False


def _assert_unknown_rebuild_views_safe(
    *,
    mf: _MigrationFile,
    sql: str,
    before: dict[tuple[str, str], _SchemaDependent],
) -> None:
    """Block a rebuild before SQLite can rewrite an unknown view definition."""

    allowed_changes = _allowed_rebuild_changes(mf, sql)
    affected = sorted(
        dependent.name
        for key, dependent in before.items()
        if dependent.object_type == "view"
        and key not in allowed_changes
        and _view_references_rebuilt_table(dependent.sql, sql)
    )
    if affected:
        names = ", ".join(f"view:{name}" for name in affected)
        raise RuntimeError(f"table rebuild would discard or alter schema objects: {names}")


async def _rebuild_dependents(
    conn: aiosqlite.Connection,
    sql: str,
) -> dict[tuple[str, str], _SchemaDependent]:
    """Snapshot explicit indexes/triggers and every potentially rewritten view."""

    tables = _affected_table_names(sql)
    if not tables:
        return {}
    placeholders = ",".join("?" for _ in tables)
    cur = await conn.execute(
        f"""SELECT type, name, tbl_name, sql FROM sqlite_schema
            WHERE sql IS NOT NULL AND (
                (type IN ('index', 'trigger') AND tbl_name IN ({placeholders}))
                OR type = 'view'
            )""",
        tables,
    )
    rows = await cur.fetchall()
    await cur.close()
    return {
        (str(row[0]), str(row[1])): _SchemaDependent(
            object_type=str(row[0]),
            name=str(row[1]),
            table_name=str(row[2]),
            sql=str(row[3]),
        )
        for row in rows
    }


async def _assert_rebuild_dependents_preserved(
    conn: aiosqlite.Connection,
    *,
    mf: _MigrationFile,
    sql: str,
    before: dict[tuple[str, str], _SchemaDependent],
) -> None:
    """Fail atomically instead of silently changing a schema dependent."""

    if not before:
        return
    allowed_changes = _allowed_rebuild_changes(mf, sql)
    changed: list[str] = []
    for key, dependent in before.items():
        if key in allowed_changes:
            continue
        cur = await conn.execute(
            "SELECT type, tbl_name, sql FROM sqlite_schema WHERE name = ?",
            (dependent.name,),
        )
        row = await cur.fetchone()
        await cur.close()
        definition_changed = (
            dependent.object_type == "view" and row is not None and str(row[2]) != dependent.sql
        )
        if (
            row is None
            or str(row[0]) != dependent.object_type
            or str(row[1]) != dependent.table_name
            or definition_changed
        ):
            changed.append(f"{dependent.object_type}:{dependent.name}")
    if changed:
        names = ", ".join(sorted(changed))
        raise RuntimeError(f"table rebuild would discard or alter schema objects: {names}")


async def _apply_one(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
    files: list[_MigrationFile],
) -> bool:
    """Atomically apply one version after rechecking it under a write lock.

    Returns ``True`` when this caller applied the version and ``False`` when a
    concurrent migrator had already committed it. SQL files must not contain
    transaction-control statements; this function owns the transaction.
    """

    sql = mf.sql
    statements = tuple(_iter_sql_statements(sql))
    _assert_no_transaction_control(statements)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cur = await conn.execute(
            "SELECT 1 FROM schema_version WHERE version = ?",
            (mf.version,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is not None:
            await _assert_registered_migration_tx(conn, mf)
            await conn.commit()
            return False
        await _prepare_published_legacy_schema_tx(conn, mf)
        rebuild_dependents = await _rebuild_dependents(conn, sql)
        _assert_unknown_rebuild_views_safe(
            mf=mf,
            sql=sql,
            before=rebuild_dependents,
        )
        for statement in statements:
            if await _published_legacy_statement_already_satisfied(conn, mf, statement):
                continue
            await conn.execute(statement)
        await _finalize_published_legacy_schema_tx(conn, mf)
        await _assert_rebuild_dependents_preserved(
            conn,
            mf=mf,
            sql=sql,
            before=rebuild_dependents,
        )
        await _insert_schema_version_tx(conn, mf)
        await _backfill_migration_checksums_tx(conn, files)
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return True


async def _rollback_quiet(conn: aiosqlite.Connection) -> None:
    """容忍 caller 二次 ROLLBACK 时已经没有活跃事务的情形。"""
    try:
        await conn.execute("ROLLBACK")
    except Exception as e:  # pragma: no cover - 仅日志
        logger.debug("rollback noop: %s", e)


async def run_migrations(  # noqa: PLR0912, PLR0915 - explicit fail-closed orchestration
    db_path: Path | str,
    *,
    migrations_dir: Path | None = None,
) -> MigrationResult:
    """对 ``db_path`` 跑全部待应用的 migration，返回结构化结果。

    参数：
        db_path: SQLite 文件路径；父目录会被自动创建。
        migrations_dir: 默认 ``<package>/migrations/``；测试可注入临时目录。

    返回：
        :class:`MigrationResult`，调用方可据 ``errors`` 决定是否中止启动。
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    mdir = migrations_dir or _DEFAULT_MIGRATIONS_DIR

    result = MigrationResult()
    files = _discover(mdir)
    if not files:
        logger.info("migrator: no migration files under %s", mdir)

    async with open_aiosqlite_connection(db_path) as conn:
        await configure_aiosqlite_connection(conn)
        await conn.execute(f"PRAGMA busy_timeout={_MIGRATION_BUSY_TIMEOUT_MS}")
        await _ensure_bootstrap(conn)
        applied = await _applied_versions(conn)

        restored_applied = applied & _RESTORED_HISTORICAL_VERSIONS
        if (
            any(version > max(_RESTORED_HISTORICAL_VERSIONS) for version in applied)
            and restored_applied
            and restored_applied != _RESTORED_HISTORICAL_VERSIONS
        ):
            msg = (
                "migration integrity: restored historical catalog is partially registered: "
                f"{sorted(restored_applied)}"
            )
            result.errors.append(msg)
            logger.error("migrator: failed %s", msg)

        if not result.errors:
            try:
                await _assert_applied_catalog(conn, files)
            except _MigrationIntegrityError as e:
                msg = f"migration integrity: {e}"
                result.errors.append(msg)
                logger.error("migrator: failed %s", msg)

        if not result.errors:
            for mf in files:
                if mf.version in applied:
                    result.skipped.append(mf.version)
                    continue
                if _restored_history_not_applicable(mf, applied):
                    result.not_applicable.append(mf.version)
                    continue
                try:
                    if await _apply_one(conn, mf, files):
                        result.applied.append(mf.version)
                        logger.info("migrator: applied v%d (%s)", mf.version, mf.name)
                    else:
                        result.skipped.append(mf.version)
                except Exception as e:
                    await _rollback_quiet(conn)
                    msg = f"v{mf.version} ({mf.name}): {e}"
                    result.errors.append(msg)
                    logger.error("migrator: failed %s", msg)
                    # 不再继续：后续 migration 可能依赖失败的 schema 状态
                    break

        if not result.errors:
            try:
                await _assert_applied_catalog(conn, files)
            except _MigrationIntegrityError as e:
                msg = f"migration integrity: {e}"
                result.errors.append(msg)
                logger.error("migrator: failed %s", msg)

        # current_version：最新成功应用 / 已存在的最大版本
        applied_now = await _applied_versions(conn)
        result.current_version = max(applied_now, default=0)
        quarantine_exists = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='migration_orphan_quarantine'"
            )
        ).fetchone()
        if quarantine_exists is not None:
            row = await (
                await conn.execute("SELECT COUNT(*) FROM migration_orphan_quarantine")
            ).fetchone()
            result.orphan_quarantined = int(row[0]) if row else 0
            if result.orphan_quarantined:
                logger.warning(
                    "migrator: quarantined %d historical orphan relations",
                    result.orphan_quarantined,
                )

    return result


__all__ = ["MigrationResult", "run_migrations"]
