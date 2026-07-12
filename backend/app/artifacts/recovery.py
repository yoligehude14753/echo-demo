"""把早期 ``skill_build`` 里的真实产物补录到 0.3 Artifact 事实源。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.artifacts.repository import ArtifactRepository
from app.artifacts.staging import cleanup_abandoned_builds, is_workflow_managed_build
from app.config import Settings
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact, normalize_kind
from app.security.models import LEGACY_OWNER_ID, LEGACY_TENANT_ID
from app.security.scope import physical_resource_id_for, scoped_directory_for

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_CLEANUP_ROOT_SKILL_BUILD = "skill_build"
_CLEANUP_ROOT_STORAGE = "storage"
_ONLINE_CLEANUP_MAX_TREE_ENTRIES = 4096
_EXT_KIND = {
    ".docx": "word",
    ".html": "html",
    ".md": "markdown",
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".txt": "txt",
    ".xlsx": "xlsx",
}


def _read_object_text(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cleanup_roots(settings: Settings) -> dict[str, Path]:
    return {
        _CLEANUP_ROOT_SKILL_BUILD: Path(settings.skill_executor_build_dir).expanduser().resolve(),
        _CLEANUP_ROOT_STORAGE: Path(settings.storage_dir).expanduser().resolve(),
    }


def _artifact_build_directories(
    root: Path,
    artifact_id: str,
    *,
    tenant_id: str,
    owner_id: str,
) -> tuple[Path, ...]:
    directories = [scoped_directory_for(root, tenant_id, owner_id).resolve() / artifact_id]
    if (tenant_id, owner_id) == (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
        directories.append(root / artifact_id)
    return tuple(directories)


def _agent_storage_candidate(
    root: Path,
    artifact_id: str,
    *,
    tenant_id: str,
    owner_id: str,
    agent_task_id: object,
    agent_relpath: object,
) -> Path | None:
    if not isinstance(agent_task_id, str) or not agent_task_id:
        return None
    if not isinstance(agent_relpath, str) or not agent_relpath:
        return None
    relpath = PurePosixPath(agent_relpath)
    parts = tuple(part for part in relpath.parts if part not in ("", "."))
    if relpath.is_absolute() or not parts or any(part == ".." for part in parts):
        return None
    expected_id = (
        f"agent-{hashlib.sha1(f'{agent_task_id}:{agent_relpath}'.encode()).hexdigest()[:24]}"
    )
    if artifact_id != expected_id:
        return None
    task_dir = physical_resource_id_for(
        agent_task_id,
        kind="agent-task",
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    return (
        scoped_directory_for(root / "agent_artifacts", tenant_id, owner_id).resolve()
        / task_dir
        / Path(*parts)
    )


def artifact_file_cleanup_target(  # noqa: PLR0911,PLR0912 - explicit fail-closed path gates
    settings: Settings,
    *,
    artifact_id: str,
    file_path: str,
    tenant_id: str,
    owner_id: str,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, str] | None:
    """Encode one validated file path as a durable controlled-root locator."""

    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    lexical_path = Path(os.path.abspath(path))
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved != lexical_path or lexical_path.is_symlink():
        return None
    for root_name, root in _cleanup_roots(settings).items():
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return None
        target = {
            "artifact_id": artifact_id,
            "root": root_name,
            "relative_path": relative.as_posix(),
        }
        if root_name == _CLEANUP_ROOT_SKILL_BUILD:
            if not any(
                resolved != build_dir and build_dir in resolved.parents
                for build_dir in _artifact_build_directories(
                    root,
                    artifact_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            ):
                return None
        elif root_name == _CLEANUP_ROOT_STORAGE:
            raw_metadata = metadata or {}
            expected = _agent_storage_candidate(
                root,
                artifact_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                agent_task_id=raw_metadata.get("agent_task_id"),
                agent_relpath=raw_metadata.get("relpath"),
            )
            if expected is not None:
                if resolved != expected:
                    return None
                target.update(
                    {
                        "binding": "agent-storage-v1",
                        "agent_task_id": str(raw_metadata["agent_task_id"]),
                        "agent_relpath": str(raw_metadata["relpath"]),
                    }
                )
            elif (tenant_id, owner_id) != (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
                # Public storage artifacts are accepted only when their path is
                # reproducible from immutable registration metadata.  Merely
                # residing somewhere in the same owner scope is not enough for
                # a resource-specific download/share capability.
                return None
        return target
    return None


def _decode_cleanup_target(  # noqa: PLR0911 - each unsafe locator shape fails closed
    settings: Settings,
    raw: object,
    *,
    tenant_id: str,
    owner_id: str,
) -> tuple[str, str, str, Path] | None:
    if not isinstance(raw, dict):
        return None
    artifact_id = raw.get("artifact_id")
    root_name = raw.get("root")
    raw_relative = raw.get("relative_path")
    if (
        not isinstance(artifact_id, str)
        or not _ARTIFACT_ID_RE.fullmatch(artifact_id)
        or not isinstance(root_name, str)
        or not isinstance(raw_relative, str)
    ):
        return None
    root = _cleanup_roots(settings).get(root_name)
    if root is None:
        return None
    relative = PurePosixPath(raw_relative)
    parts = tuple(part for part in relative.parts if part not in ("", "."))
    if relative.is_absolute() or not parts or any(part == ".." for part in parts):
        return None
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    # Reject both a symlink leaf and any symlinked parent. Durable locators are
    # lexical paths under the controlled root, not capabilities to follow links.
    if resolved != candidate or candidate.is_symlink():
        return None
    if root_name == _CLEANUP_ROOT_SKILL_BUILD:
        if not any(
            candidate == build_dir or build_dir in candidate.parents
            for build_dir in _artifact_build_directories(
                root,
                artifact_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        ):
            return None
    elif root_name == _CLEANUP_ROOT_STORAGE:
        if raw.get("binding") == "agent-storage-v1":
            expected = _agent_storage_candidate(
                root,
                artifact_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                agent_task_id=raw.get("agent_task_id"),
                agent_relpath=raw.get("agent_relpath"),
            )
            if expected is None or candidate != expected:
                return None
        elif (tenant_id, owner_id) != (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
            return None
    return artifact_id, root_name, PurePosixPath(*parts).as_posix(), candidate


ArtifactCleanupReplayOutcome = Literal["deleted", "absent", "protected", "unsafe"]


async def _protected_cleanup_paths_for_scope(
    conn: aiosqlite.Connection,
    settings: Settings,
    *,
    tenant_id: str,
    owner_id: str,
) -> set[Path]:
    if not conn.in_transaction:
        raise RuntimeError("artifact cleanup protection requires an active write transaction")
    cur = await conn.execute(
        """SELECT artifact_id, file_path, metadata_json FROM artifacts
           WHERE tenant_id = ? AND owner_id = ?""",
        (tenant_id, owner_id),
    )
    rows = await cur.fetchall()
    await cur.close()
    protected: set[Path] = set()
    for artifact_id, file_path, metadata_json in rows:
        registered_path = Path(str(file_path)).expanduser()
        if not registered_path.is_absolute():
            registered_path = Path.cwd() / registered_path
        lexical_path = Path(os.path.abspath(registered_path))
        if any(
            lexical_path != root and root in lexical_path.parents
            for root in _cleanup_roots(settings).values()
        ):
            # A newly registered artifact may intentionally reuse a path whose
            # old artifact-id binding no longer matches.  Protection is
            # conservative: lexical containment can only prevent deletion.
            protected.add(lexical_path)
        encoded = artifact_file_cleanup_target(
            settings,
            artifact_id=str(artifact_id),
            file_path=str(file_path),
            tenant_id=tenant_id,
            owner_id=owner_id,
            metadata=_read_object_text(str(metadata_json)),
        )
        decoded = _decode_cleanup_target(
            settings,
            encoded,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if decoded is not None:
            protected.add(decoded[3])
    return protected


def _online_cleanup_tree_is_bounded(build_dir: Path) -> bool:
    """Bound recursive filesystem work while the SQLite writer lock is held."""

    pending = [build_dir]
    entries = 0
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > _ONLINE_CLEANUP_MAX_TREE_ENTRIES:
                        return False
                    if child.is_dir(follow_symlinks=False):
                        pending.append(Path(child.path))
    except OSError:
        return False
    return True


def _replay_cleanup_target_under_lock(  # noqa: PLR0911, PLR0912 - explicit outcomes
    settings: Settings,
    *,
    artifact_id: str,
    root_name: str,
    candidate: Path,
    tenant_id: str,
    owner_id: str,
    protected_paths: set[Path],
) -> ArtifactCleanupReplayOutcome:
    if root_name == _CLEANUP_ROOT_SKILL_BUILD:
        root = _cleanup_roots(settings)[_CLEANUP_ROOT_SKILL_BUILD]
        matched = False
        for build_dir in _artifact_build_directories(
            root,
            artifact_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        ):
            if candidate != build_dir and build_dir not in candidate.parents:
                continue
            matched = True
            if any(
                protected == build_dir or build_dir in protected.parents
                for protected in protected_paths
            ):
                return "protected"
            if build_dir.is_symlink():
                return "unsafe"
            try:
                resolved_build_dir = build_dir.resolve()
            except OSError:
                return "unsafe"
            if resolved_build_dir != build_dir:
                return "unsafe"
            if not build_dir.exists():
                continue
            if not build_dir.is_dir() or not _online_cleanup_tree_is_bounded(build_dir):
                return "unsafe"
            try:
                shutil.rmtree(build_dir)
            except FileNotFoundError:
                return "absent"
            return "deleted"
        return "absent" if matched else "unsafe"

    if candidate in protected_paths:
        return "protected"
    if candidate.is_symlink():
        return "unsafe"
    try:
        resolved = candidate.resolve()
    except OSError:
        return "unsafe"
    if resolved != candidate:
        return "unsafe"
    if not candidate.exists():
        return "absent"
    if candidate.is_dir():
        return "unsafe"
    try:
        candidate.unlink()
    except FileNotFoundError:
        return "absent"
    return "deleted"


async def replay_artifact_file_cleanup_target(
    settings: Settings,
    target: object,
    *,
    tenant_id: str,
    owner_id: str,
) -> ArtifactCleanupReplayOutcome:
    """Replay one durable target without deleting a reused or unsafe path."""

    decoded = _decode_cleanup_target(
        settings,
        target,
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    if decoded is None:
        return "unsafe"
    artifact_id, root_name, _relative_path, candidate = decoded
    db_path = Path(settings.db_path).expanduser()
    if not db_path.is_file():
        return "unsafe"
    async with open_aiosqlite_connection(db_path) as conn:
        await configure_aiosqlite_connection(conn)
        await conn.execute("BEGIN IMMEDIATE")
        try:
            protected_paths = await _protected_cleanup_paths_for_scope(
                conn,
                settings,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            outcome = _replay_cleanup_target_under_lock(
                settings,
                artifact_id=artifact_id,
                root_name=root_name,
                candidate=candidate,
                tenant_id=tenant_id,
                owner_id=owner_id,
                protected_paths=protected_paths,
            )
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()
    return outcome


def validated_artifact_file_path(
    settings: Settings,
    *,
    artifact_id: str,
    file_path: str,
    tenant_id: str,
    owner_id: str,
    metadata: Mapping[str, object] | None = None,
) -> Path | None:
    """Resolve a regular artifact file through the shared scoped locator fence."""

    target = artifact_file_cleanup_target(
        settings,
        artifact_id=artifact_id,
        file_path=file_path,
        tenant_id=tenant_id,
        owner_id=owner_id,
        metadata=metadata,
    )
    decoded = _decode_cleanup_target(
        settings,
        target,
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    if decoded is None:
        return None
    candidate = decoded[3]
    return candidate if candidate.is_file() and not candidate.is_symlink() else None


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryReport:
    discovered: int = 0
    recovered: int = 0
    linked: int = 0
    already_recorded: int = 0
    skipped: int = 0
    workflow_managed: int = 0
    abandoned_builds_cleaned: int = 0


def _read_meta(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _todo_links(meetings: Sequence[object]) -> dict[str, tuple[str, str | None]]:
    links: dict[str, tuple[str, str | None]] = {}
    for meeting in meetings:
        meeting_id = str(getattr(meeting, "id", ""))
        minutes_json = getattr(meeting, "minutes_json", None)
        if not meeting_id or not isinstance(minutes_json, str):
            continue
        try:
            minutes = json.loads(minutes_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(minutes, dict):
            continue
        for todo in minutes.get("todos", []) or []:
            if not isinstance(todo, dict):
                continue
            artifact_id = todo.get("artifact_id")
            if isinstance(artifact_id, str) and _ARTIFACT_ID_RE.fullmatch(artifact_id):
                todo_id = todo.get("id")
                links.setdefault(
                    artifact_id,
                    (meeting_id, str(todo_id) if isinstance(todo_id, str) else None),
                )
    return links


def _is_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _candidate(root: Path, directory: Path) -> tuple[GeneratedArtifact, str | None] | None:
    artifact_id = directory.name
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None
    if not _is_inside(directory, root):
        return None
    outputs = sorted(path for path in directory.glob("output.*") if path.is_file())
    if not outputs:
        return None
    output = outputs[0]
    if not _is_inside(output, root):
        return None
    meta = _read_meta(directory / "meta.json")
    raw_kind = str(meta.get("artifact_type") or "")
    artifact_type = normalize_kind(raw_kind) or _EXT_KIND.get(output.suffix.lower(), "")
    if not artifact_type:
        return None
    meeting_id = meta.get("meeting_id")
    metadata = {
        "recovered": "true",
        "recovery_source": "skill_build",
        "original_build_dir": str(directory.resolve()),
    }
    if isinstance(meeting_id, str):
        metadata["meeting_id"] = meeting_id
    return (
        GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            title=str(meta.get("title") or artifact_id),
            file_path=str(output.resolve()),
            mime_type=mimetypes.guess_type(output.name)[0] or "application/octet-stream",
            size_bytes=output.stat().st_size,
            generation_latency_ms=0,
            model="legacy-recovered",
            metadata=metadata,
        ),
        meeting_id if isinstance(meeting_id, str) else None,
    )


async def recover_skill_build_artifacts(
    *,
    settings: Settings,
    repository: RepositoryPort,
    artifact_repo: ArtifactRepository,
) -> ArtifactRecoveryReport:
    """幂等扫描已有构建目录，并补齐 metadata 与会议关联。

    早期版本已把文件和 ``meta.json`` 写入磁盘，但只把 artifact_id 留在
    ``meetings.minutes_json.todos``。0.3 的 ``artifacts`` / ``artifact_links`` 表
    新增后必须从两处事实自动恢复，不能要求用户逐条重新生成。
    """
    root = Path(settings.skill_executor_build_dir).expanduser().resolve()
    if not root.is_dir():
        return ArtifactRecoveryReport()

    abandoned_builds_cleaned = await cleanup_abandoned_builds(settings)

    meetings = await repository.list_meetings(limit=10_000)
    known_meeting_ids = {meeting.id for meeting in meetings}
    todo_links = _todo_links(meetings)
    discovered = recovered = linked = already_recorded = skipped = workflow_managed = 0

    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        if is_workflow_managed_build(directory):
            workflow_managed += 1
            continue
        candidate = _candidate(root, directory)
        if candidate is None:
            skipped += 1
            continue
        discovered += 1
        artifact, meta_meeting_id = candidate
        if await artifact_repo.get_artifact(artifact.artifact_id) is not None:
            already_recorded += 1
        else:
            await artifact_repo.save_artifact(artifact)
            recovered += 1

        meeting_link = None
        todo_id = None
        if artifact.artifact_id in todo_links:
            meeting_link, todo_id = todo_links[artifact.artifact_id]
        if meta_meeting_id in known_meeting_ids:
            meeting_link = meta_meeting_id
        if meeting_link:
            existing_links = await artifact_repo.list_links_for_artifact(artifact.artifact_id)
            if not any(
                item.meeting_id == meeting_link and item.todo_id == todo_id
                for item in existing_links
            ):
                await artifact_repo.link_artifact(
                    artifact_id=artifact.artifact_id,
                    source="legacy_skill_build_recovery",
                    meeting_id=meeting_link,
                    todo_id=todo_id,
                )
                linked += 1

    return ArtifactRecoveryReport(
        discovered=discovered,
        recovered=recovered,
        linked=linked,
        already_recorded=already_recorded,
        skipped=skipped,
        workflow_managed=workflow_managed,
        abandoned_builds_cleaned=abandoned_builds_cleaned,
    )


async def replay_succeeded_artifact_file_cleanups(  # noqa: PLR0912, PLR0915 - two durable formats plus safety fences
    settings: Settings,
) -> int:
    """Idempotently finish post-commit file deletion after an earlier crash.

    ``meeting.outputs.clear`` deletes links/metadata in its SQLite Unit of Work
    and records artifact ids in the succeeded workflow output.  Filesystem
    deletion happens after commit, so startup replays the durable cleanup intent.
    """

    db_path = Path(settings.db_path).expanduser()
    if not db_path.is_file():
        return 0
    cleanup_targets: set[tuple[str, str, str, str, str, str, str, str]] = set()
    artifact_ids: set[tuple[str, str, str]] = set()
    targeted_artifact_ids: set[tuple[str, str, str]] = set()
    protected_artifact_ids: set[tuple[str, str, str]] = set()
    async with open_aiosqlite_connection(db_path) as conn:
        await configure_aiosqlite_connection(conn)
        cur = await conn.execute(
            """SELECT tenant_id, owner_id, output_json FROM workflow_runs
               WHERE kind = 'meeting.outputs.clear' AND state = 'succeeded'"""
        )
        rows = await cur.fetchall()
        await cur.close()
        cur = await conn.execute(
            "SELECT tenant_id, owner_id, artifact_id, file_path, metadata_json FROM artifacts"
        )
        artifact_rows = await cur.fetchall()
        protected_artifact_ids = {(str(row[0]), str(row[1]), str(row[2])) for row in artifact_rows}
        await cur.close()
    for tenant_id, owner_id, raw_output in rows:
        scope = (str(tenant_id), str(owner_id))
        try:
            output = json.loads(str(raw_output))
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict):
            continue
        ids = output.get("file_cleanup_artifact_ids")
        if isinstance(ids, list):
            artifact_ids.update(
                (scope[0], scope[1], artifact_id)
                for artifact_id in ids
                if isinstance(artifact_id, str) and _ARTIFACT_ID_RE.fullmatch(artifact_id)
            )
        targets = output.get("file_cleanup_targets")
        if not isinstance(targets, list):
            continue
        for raw_target in targets:
            decoded = _decode_cleanup_target(
                settings,
                raw_target,
                tenant_id=scope[0],
                owner_id=scope[1],
            )
            if decoded is None:
                continue
            artifact_id, root_name, relative_path, _candidate_path = decoded
            cleanup_targets.add(
                (
                    scope[0],
                    scope[1],
                    artifact_id,
                    root_name,
                    relative_path,
                    str(raw_target.get("binding") or ""),
                    str(raw_target.get("agent_task_id") or ""),
                    str(raw_target.get("agent_relpath") or ""),
                )
            )
            targeted_artifact_ids.add((scope[0], scope[1], artifact_id))

    removed = 0
    for (
        tenant_id,
        owner_id,
        artifact_id,
        root_name,
        relative_path,
        binding,
        agent_task_id,
        agent_relpath,
    ) in sorted(cleanup_targets):
        if (tenant_id, owner_id, artifact_id) in protected_artifact_ids:
            continue
        replay_target = {
            "artifact_id": artifact_id,
            "root": root_name,
            "relative_path": relative_path,
        }
        if binding:
            replay_target.update(
                {
                    "binding": binding,
                    "agent_task_id": agent_task_id,
                    "agent_relpath": agent_relpath,
                }
            )
        outcome = await replay_artifact_file_cleanup_target(
            settings,
            replay_target,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        removed += int(outcome == "deleted")

    root = _cleanup_roots(settings)[_CLEANUP_ROOT_SKILL_BUILD]
    legacy_only_ids = artifact_ids - protected_artifact_ids - targeted_artifact_ids
    for tenant_id, owner_id, artifact_id in sorted(legacy_only_ids):
        scope_root = scoped_directory_for(root, tenant_id, owner_id).resolve()
        candidates = [scope_root / artifact_id]
        if (tenant_id, owner_id) == (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
            candidates.append(root / artifact_id)
        for candidate in candidates:
            try:
                relative_path = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            outcome = await replay_artifact_file_cleanup_target(
                settings,
                {
                    "artifact_id": artifact_id,
                    "root": _CLEANUP_ROOT_SKILL_BUILD,
                    "relative_path": relative_path,
                },
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            deleted = int(outcome == "deleted")
            removed += deleted
            if deleted:
                break
    return removed
