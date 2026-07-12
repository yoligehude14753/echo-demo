"""Durable filesystem staging for ``artifact.generate`` workflow runs.

Artifact bytes cannot participate in the SQLite Unit of Work.  This module
therefore gives every workflow run one deterministic build directory and a
small atomic manifest.  A restored run can reuse the already published bytes
instead of executing generated host code a second time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact, normalize_kind
from app.security.context import current_principal
from app.security.models import LEGACY_OWNER_ID, LEGACY_TENANT_ID
from app.security.scope import (
    SCOPES_DIRECTORY,
    scope_storage_key_for,
    scoped_directory,
    scoped_directory_for,
)

WORKFLOW_BUILDING_DIR = ".workflow-building"
WORKFLOW_MANIFEST = ".workflow-artifact.json"
_WORKFLOW_ARTIFACT_ID_RE = re.compile(r"^[a-z]+-run-[0-9a-f]{20}$")
_PRIVATE_BUILD_DIR_RE = re.compile(r"^([a-z]+-run-[0-9a-f]{20})-.+$")
_ACTIVE_MARKER_PREFIX = ".workflow-active-"
_ACTIVE_MARKER_SUFFIX = ".json"
_QUARANTINE_DIR = ".workflow-quarantine"
_PRINCIPAL_PAGE_SIZE = 128


@dataclass(frozen=True, slots=True)
class _ActiveBuildMarker:
    artifact_id: str
    run_id: str
    tenant_id: str
    owner_id: str
    fence_token: int


def workflow_artifact_id(run_id: str, artifact_type: str) -> str:
    """Return the stable artifact/build-directory id for a workflow run."""

    kind = normalize_kind(artifact_type) or "artifact"
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
    return f"{kind}-run-{digest}"


def workflow_build_dir(settings: Settings, run_id: str, artifact_type: str) -> Path:
    root = scoped_directory(settings.skill_executor_build_dir).resolve()
    return root / workflow_artifact_id(run_id, artifact_type)


def _active_marker_path(building_root: Path, artifact_id: str) -> Path:
    return building_root / f"{_ACTIVE_MARKER_PREFIX}{artifact_id}{_ACTIVE_MARKER_SUFFIX}"


@contextmanager
def workflow_build_lease_marker(
    settings: Settings,
    *,
    run_id: str,
    artifact_type: str,
    fence_token: int,
) -> Iterator[None]:
    """Publish the active workflow lease before the executor creates bytes.

    The marker maps the executor's random private directory back to the
    durable ``execution_leases`` row.  Startup cleanup can therefore distinguish
    another live process from a genuinely abandoned build without guessing
    from age.  Hand-written/unit handlers with no real fence use the legacy
    stale-grace path instead.
    """

    if fence_token <= 0:
        yield
        return
    artifact_id = workflow_artifact_id(run_id, artifact_type)
    principal = current_principal()
    building_root = (
        scoped_directory(settings.skill_executor_build_dir).resolve() / WORKFLOW_BUILDING_DIR
    )
    building_root.mkdir(parents=True, exist_ok=True)
    marker = _active_marker_path(building_root, artifact_id)
    payload = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "run_id": run_id,
        "tenant_id": principal.tenant_id,
        "owner_id": principal.owner_id,
        "fence_token": fence_token,
    }
    tmp = marker.with_name(f"{marker.name}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, marker)
    try:
        yield
    finally:
        current = _read_object(marker)
        if current.get("run_id") == run_id and current.get("fence_token") == fence_token:
            with contextlib.suppress(OSError):
                marker.unlink()


def _safe_workflow_build_dir(settings: Settings, run_id: str, artifact_type: str) -> Path | None:
    root = scoped_directory(settings.skill_executor_build_dir).resolve()
    candidate = root / workflow_artifact_id(run_id, artifact_type)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.parent == root else None


def is_workflow_managed_build(directory: Path) -> bool:
    """Keep workflow-owned outputs out of the legacy ownerless recovery path."""

    return (
        bool(_WORKFLOW_ARTIFACT_ID_RE.fullmatch(directory.name))
        or (directory / WORKFLOW_MANIFEST).is_file()
    )


def _read_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _published_output(directory: Path, preferred_name: str | None = None) -> Path | None:
    candidates: list[Path]
    if preferred_name and Path(preferred_name).name == preferred_name:
        candidates = [directory / preferred_name]
    else:
        candidates = sorted(directory.glob("output.*"))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            directory_resolved = directory.resolve(strict=True)
        except OSError:
            continue
        if resolved.parent != directory_resolved or not resolved.is_file():
            continue
        if resolved.stat().st_size <= 0:
            continue
        return resolved
    return None


def load_workflow_artifact(
    settings: Settings,
    *,
    run_id: str,
    artifact_type: str,
) -> GeneratedArtifact | None:
    """Load bytes published before a process crash, with or without a manifest.

    The manifest is normally written by the handler immediately after the
    executor atomically publishes the directory.  Falling back to ``meta.json``
    covers the narrower crash point between directory publication and manifest
    creation.
    """

    artifact_id = workflow_artifact_id(run_id, artifact_type)
    directory = _safe_workflow_build_dir(settings, run_id, artifact_type)
    if directory is None or not directory.is_dir():
        return None

    manifest = _read_object(directory / WORKFLOW_MANIFEST)
    raw_artifact = manifest.get("artifact")
    if manifest.get("run_id") == run_id and isinstance(raw_artifact, dict):
        try:
            recorded = GeneratedArtifact.model_validate(raw_artifact)
        except ValueError:
            recorded = None
        if recorded is not None and recorded.artifact_id == artifact_id:
            output = _published_output(directory, Path(recorded.file_path).name)
            if output is not None:
                metadata = dict(recorded.metadata)
                metadata["workflow_run_id"] = run_id
                return recorded.model_copy(
                    update={
                        "file_path": str(output),
                        "size_bytes": output.stat().st_size,
                        "metadata": metadata,
                    }
                )

    output = _published_output(directory)
    if output is None:
        return None
    meta = _read_object(directory / "meta.json")
    kind = normalize_kind(str(meta.get("artifact_type") or artifact_type))
    if not kind:
        return None
    return GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_type=kind,
        title=str(meta.get("title") or artifact_id),
        file_path=str(output),
        mime_type=mimetypes.guess_type(output.name)[0] or "application/octet-stream",
        size_bytes=output.stat().st_size,
        generation_latency_ms=0,
        model="workflow-crash-recovery",
        metadata={"workflow_run_id": run_id, "recovered_from_staging": "true"},
    )


def write_workflow_artifact_manifest(
    settings: Settings,
    *,
    run_id: str,
    artifact_type: str,
    artifact: GeneratedArtifact,
) -> GeneratedArtifact:
    """Validate a deterministic output and atomically publish its replay manifest."""

    artifact_id = workflow_artifact_id(run_id, artifact_type)
    directory = _safe_workflow_build_dir(settings, run_id, artifact_type)
    if directory is None:
        raise RuntimeError("artifact build directory escaped configured root")
    output = _published_output(directory, Path(artifact.file_path).name)
    if artifact.artifact_id != artifact_id or output is None:
        raise RuntimeError("artifact executor did not publish the deterministic run output")

    metadata = dict(artifact.metadata)
    metadata["workflow_run_id"] = run_id
    normalized = artifact.model_copy(
        update={
            "file_path": str(output),
            "size_bytes": output.stat().st_size,
            "metadata": metadata,
        }
    )
    portable = normalized.model_copy(update={"file_path": output.name})
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "artifact": portable.model_dump(mode="json"),
    }
    tmp = directory / f"{WORKFLOW_MANIFEST}.{uuid4().hex}.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, directory / WORKFLOW_MANIFEST)
    return normalized


def _parse_active_marker(path: Path) -> _ActiveBuildMarker | None:
    raw = _read_object(path)
    try:
        marker = _ActiveBuildMarker(
            artifact_id=str(raw["artifact_id"]),
            run_id=str(raw["run_id"]),
            tenant_id=str(raw["tenant_id"]),
            owner_id=str(raw["owner_id"]),
            fence_token=int(str(raw["fence_token"])),
        )
    except (KeyError, TypeError, ValueError):
        return None
    expected_name = f"{_ACTIVE_MARKER_PREFIX}{marker.artifact_id}{_ACTIVE_MARKER_SUFFIX}"
    artifact_type = marker.artifact_id.partition("-run-")[0]
    if (
        path.name != expected_name
        or not _WORKFLOW_ARTIFACT_ID_RE.fullmatch(marker.artifact_id)
        or not marker.run_id
        or workflow_artifact_id(marker.run_id, artifact_type) != marker.artifact_id
        or not marker.tenant_id
        or not marker.owner_id
        or marker.fence_token <= 0
    ):
        return None
    return marker


async def _has_live_workflow_lease(
    conn: aiosqlite.Connection,
    marker: _ActiveBuildMarker,
    *,
    now: float,
) -> bool:
    cur = await conn.execute(
        """SELECT 1 FROM execution_leases
           WHERE tenant_id = ? AND owner_id = ?
             AND resource_kind = 'workflow' AND resource_id = ?
             AND fence_token = ? AND expires_at > ?""",
        (
            marker.tenant_id,
            marker.owner_id,
            marker.run_id,
            marker.fence_token,
            now,
        ),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def _has_any_live_workflow_lease(
    conn: aiosqlite.Connection,
    marker: _ActiveBuildMarker,
    *,
    now: float,
) -> bool:
    """Detect a successor term even before it has replaced the old marker."""

    cur = await conn.execute(
        """SELECT 1 FROM execution_leases
           WHERE tenant_id = ? AND owner_id = ?
             AND resource_kind = 'workflow' AND resource_id = ?
             AND expires_at > ?""",
        (
            marker.tenant_id,
            marker.owner_id,
            marker.run_id,
            now,
        ),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def _delete_entry_for_inactive_marker_generation(
    conn: aiosqlite.Connection,
    entry: Path,
    marker_path: Path,
    expected: _ActiveBuildMarker,
    quarantine_root: Path,
) -> Path | None:
    """Detach one inactive generation under a short SQLite write fence.

    Recursive deletion is intentionally not done here.  Moving the private
    directory to a sibling quarantine is atomic on the same filesystem and
    makes the original name available to a successor without retaining the
    SQLite writer lock while potentially slow filesystem cleanup runs.
    """

    await conn.execute("BEGIN IMMEDIATE")
    try:
        current = _parse_active_marker(marker_path)
        if current != expected or await _has_any_live_workflow_lease(
            conn,
            expected,
            now=time.time(),
        ):
            await conn.rollback()
            return None
        # Re-read immediately before the filesystem mutation.  BEGIN IMMEDIATE
        # prevents a legal successor from acquiring a new workflow term between
        # the lease check and deletion; equality fences a marker already replaced
        # by a successor that acquired just before this transaction.
        if _parse_active_marker(marker_path) != expected:
            await conn.rollback()
            return None
        quarantine = quarantine_root / (
            f"{entry.name}.fence-{expected.fence_token}.{uuid4().hex}.pending"
        )
        try:
            os.replace(entry, quarantine)
        except FileNotFoundError:
            await conn.rollback()
            return None
        await conn.commit()
        return quarantine
    except BaseException:
        await conn.rollback()
        raise


async def _unlink_inactive_marker_generation(
    conn: aiosqlite.Connection,
    marker_path: Path,
    expected: _ActiveBuildMarker,
) -> bool:
    """Content/term compare-and-unlink for an inactive marker generation."""

    await conn.execute("BEGIN IMMEDIATE")
    try:
        current = _parse_active_marker(marker_path)
        if current != expected or await _has_any_live_workflow_lease(
            conn,
            expected,
            now=time.time(),
        ):
            await conn.rollback()
            return False
        if _parse_active_marker(marker_path) != expected:
            await conn.rollback()
            return False
        marker_path.unlink(missing_ok=True)
        await conn.commit()
        return True
    except BaseException:
        await conn.rollback()
        raise


def _entry_is_stale(path: Path, *, now: float, grace_s: float) -> bool:
    try:
        return now - path.lstat().st_mtime >= grace_s
    except OSError:
        return False


def _private_artifact_id(name: str) -> str | None:
    match = _PRIVATE_BUILD_DIR_RE.fullmatch(name)
    return match.group(1) if match is not None else None


def _marker_matches_scope(
    marker: _ActiveBuildMarker,
    *,
    tenant_id: str,
    owner_id: str,
    scope_key: str | None,
) -> bool:
    if (marker.tenant_id, marker.owner_id) != (tenant_id, owner_id):
        return False
    return (
        scope_key is None or scope_storage_key_for(marker.tenant_id, marker.owner_id) == scope_key
    )


def _safe_quarantine_root(building_root: Path, *, create: bool) -> Path | None:
    quarantine = building_root / _QUARANTINE_DIR
    try:
        if quarantine.is_symlink():
            return None
        if create:
            quarantine.mkdir(mode=0o700, exist_ok=True)
        if not quarantine.is_dir() or quarantine.is_symlink():
            return None
        resolved = quarantine.resolve()
    except OSError:
        return None
    return resolved if resolved.parent == building_root else None


async def _delete_quarantined_entry(entry: Path, quarantine_root: Path) -> bool:
    """Delete one detached entry without following a top-level symlink."""

    if entry.parent != quarantine_root:
        return False
    try:
        if entry.is_dir() and not entry.is_symlink():
            await asyncio.to_thread(shutil.rmtree, entry)
        else:
            entry.unlink(missing_ok=True)
    except Exception:
        return False
    return not entry.exists() and not entry.is_symlink()


async def _drain_quarantine(building_root: Path) -> int:
    quarantine_root = _safe_quarantine_root(building_root, create=False)
    if quarantine_root is None:
        return 0
    removed = 0
    try:
        entries = list(quarantine_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if await _delete_quarantined_entry(entry, quarantine_root):
            removed += 1
    with contextlib.suppress(OSError):
        quarantine_root.rmdir()
    return removed


def _quarantine_unmarked_entry(entry: Path, quarantine_root: Path) -> Path | None:
    if entry.parent != quarantine_root.parent or entry.name == _QUARANTINE_DIR:
        return None
    quarantine = quarantine_root / f"{entry.name}.{uuid4().hex}.pending"
    try:
        os.replace(entry, quarantine)
    except OSError:
        return None
    return quarantine


def _safe_scoped_building_root(  # noqa: PLR0911
    base: Path,
    *,
    tenant_id: str,
    owner_id: str,
) -> tuple[Path, str] | None:
    """Resolve one DB-authored scope without following scoped path symlinks."""

    scope_key = scope_storage_key_for(tenant_id, owner_id)
    scopes_root = base / SCOPES_DIRECTORY
    scope_root = scoped_directory_for(base, tenant_id, owner_id)
    building_root = scope_root / WORKFLOW_BUILDING_DIR
    try:
        if scopes_root.is_symlink() or scope_root.is_symlink() or building_root.is_symlink():
            return None
        resolved_scopes = scopes_root.resolve()
        resolved_scope = scope_root.resolve()
        resolved_building = building_root.resolve()
    except OSError:
        return None
    if resolved_scopes.parent != base or resolved_scopes.name != SCOPES_DIRECTORY:
        return None
    if resolved_scope.parent != resolved_scopes or resolved_scope.name != scope_key:
        return None
    if (
        resolved_building.parent != resolved_scope
        or resolved_building.name != WORKFLOW_BUILDING_DIR
    ):
        return None
    if not resolved_building.is_dir():
        return None
    return resolved_building, scope_key


async def _iter_persisted_principal_scopes(
    conn: aiosqlite.Connection,
) -> AsyncIterator[tuple[str, str]]:
    """Keyset-page every durable user scope without materializing the table."""

    after_tenant = ""
    after_owner = ""
    while True:
        cur = await conn.execute(
            """SELECT tenant_id, user_id FROM users
               WHERE tenant_id > ? OR (tenant_id = ? AND user_id > ?)
               ORDER BY tenant_id, user_id
               LIMIT ?""",
            (after_tenant, after_tenant, after_owner, _PRINCIPAL_PAGE_SIZE),
        )
        rows = list(await cur.fetchmany(_PRINCIPAL_PAGE_SIZE))
        await cur.close()
        if not rows:
            return
        for row in rows:
            tenant_id = str(row[0])
            owner_id = str(row[1])
            if tenant_id and owner_id:
                yield tenant_id, owner_id
        after_tenant = str(rows[-1][0])
        after_owner = str(rows[-1][1])
        if len(rows) < _PRINCIPAL_PAGE_SIZE:
            return


async def _cleanup_building_root(  # noqa: PLR0912, PLR0915
    conn: aiosqlite.Connection,
    building_root: Path,
    *,
    tenant_id: str,
    owner_id: str,
    scope_key: str | None,
    now: float,
    grace_s: float,
) -> int:
    """Clean one already validated principal building root."""

    removed = await _drain_quarantine(building_root)
    try:
        snapshot = list(building_root.iterdir())
    except OSError:
        return removed

    marker_states: dict[str, bool] = {}
    marker_paths: dict[str, Path] = {}
    marker_generations: dict[str, _ActiveBuildMarker] = {}
    invalid_markers: set[Path] = set()
    mismatched_markers: set[Path] = set()
    protected_artifact_ids: set[str] = set()
    for path in snapshot:
        if not (
            path.name.startswith(_ACTIVE_MARKER_PREFIX)
            and path.name.endswith(_ACTIVE_MARKER_SUFFIX)
        ):
            continue
        marker = _parse_active_marker(path)
        if marker is None:
            raw_id = path.name.removeprefix(_ACTIVE_MARKER_PREFIX).removesuffix(
                _ACTIVE_MARKER_SUFFIX
            )
            if _WORKFLOW_ARTIFACT_ID_RE.fullmatch(raw_id):
                protected_artifact_ids.add(raw_id)
                mismatched_markers.add(path)
            else:
                invalid_markers.add(path)
            continue
        if not _marker_matches_scope(
            marker,
            tenant_id=tenant_id,
            owner_id=owner_id,
            scope_key=scope_key,
        ):
            mismatched_markers.add(path)
            protected_artifact_ids.add(marker.artifact_id)
            continue
        marker_paths[marker.artifact_id] = path
        marker_generations[marker.artifact_id] = marker
        marker_states[marker.artifact_id] = await _has_live_workflow_lease(
            conn,
            marker,
            now=now,
        )

    try:
        children = list(building_root.iterdir())
    except OSError:
        return removed
    known_marker_paths = marker_paths.values()
    for child in children:
        if (
            child.name == _QUARANTINE_DIR
            or child in known_marker_paths
            or child in invalid_markers
            or child in mismatched_markers
        ):
            continue
        matching_id = _private_artifact_id(child.name)
        if matching_id in protected_artifact_ids:
            continue
        if matching_id is not None and matching_id in marker_states:
            if marker_states[matching_id]:
                continue
            quarantine_root = _safe_quarantine_root(building_root, create=True)
            if quarantine_root is None:
                continue
            quarantine = await _delete_entry_for_inactive_marker_generation(
                conn,
                child,
                marker_paths[matching_id],
                marker_generations[matching_id],
                quarantine_root,
            )
            if quarantine is not None and await _delete_quarantined_entry(
                quarantine,
                quarantine_root,
            ):
                removed += 1
            continue
        if matching_id is not None and _active_marker_path(building_root, matching_id).exists():
            # A producer publishes its marker before its private bytes.  A marker
            # that appeared after the snapshot fences this entry until next run.
            continue
        if not _entry_is_stale(child, now=now, grace_s=grace_s):
            continue
        quarantine_root = _safe_quarantine_root(building_root, create=True)
        if quarantine_root is None:
            continue
        quarantine = _quarantine_unmarked_entry(child, quarantine_root)
        if quarantine is not None and await _delete_quarantined_entry(
            quarantine,
            quarantine_root,
        ):
            removed += 1

    for artifact_id, marker_path in marker_paths.items():
        if marker_states[artifact_id]:
            continue
        await _unlink_inactive_marker_generation(
            conn,
            marker_path,
            marker_generations[artifact_id],
        )
    for invalid in invalid_markers:
        if _entry_is_stale(invalid, now=now, grace_s=grace_s):
            with contextlib.suppress(OSError):
                invalid.unlink()
    with contextlib.suppress(OSError):
        building_root.rmdir()
    return removed


async def cleanup_abandoned_builds(settings: Settings) -> int:
    """Remove lease-expired builds across every durable principal scope."""

    removed = 0
    now = time.time()
    grace_s = settings.artifact_build_stale_grace_s
    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    try:
        async with open_aiosqlite_connection(settings.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            legacy_root = base / WORKFLOW_BUILDING_DIR
            if (
                not legacy_root.is_symlink()
                and legacy_root.is_dir()
                and legacy_root.resolve().parent == base
            ):
                removed += await _cleanup_building_root(
                    conn,
                    legacy_root.resolve(),
                    tenant_id=LEGACY_TENANT_ID,
                    owner_id=LEGACY_OWNER_ID,
                    scope_key=None,
                    now=now,
                    grace_s=grace_s,
                )
            async for tenant_id, owner_id in _iter_persisted_principal_scopes(conn):
                scoped = _safe_scoped_building_root(
                    base,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                if scoped is None:
                    continue
                building_root, scope_key = scoped
                removed += await _cleanup_building_root(
                    conn,
                    building_root,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    scope_key=scope_key,
                    now=now,
                    grace_s=grace_s,
                )
    except (OSError, aiosqlite.Error):
        # Fail safe: inability to prove that a lease is inactive must never
        # turn startup recovery into deletion of another process's live bytes.
        return 0
    return removed
