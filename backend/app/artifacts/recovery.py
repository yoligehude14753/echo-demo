"""把早期 ``skill_build`` 里的真实产物补录到 0.3 Artifact 事实源。"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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
from app.security.scope import scoped_directory_for

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_EXT_KIND = {
    ".docx": "word",
    ".html": "html",
    ".md": "markdown",
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".txt": "txt",
    ".xlsx": "xlsx",
}


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


async def replay_succeeded_artifact_file_cleanups(settings: Settings) -> int:
    """Idempotently finish post-commit file deletion after an earlier crash.

    ``meeting.outputs.clear`` deletes links/metadata in its SQLite Unit of Work
    and records artifact ids in the succeeded workflow output.  Filesystem
    deletion happens after commit, so startup replays the durable cleanup intent.
    """

    root = Path(settings.skill_executor_build_dir).expanduser().resolve()
    if not root.is_dir() or not Path(settings.db_path).is_file():
        return 0
    artifact_ids: set[tuple[str, str, str]] = set()
    protected_artifact_ids: set[tuple[str, str, str]] = set()
    async with open_aiosqlite_connection(settings.db_path) as conn:
        await configure_aiosqlite_connection(conn)
        cur = await conn.execute(
            """SELECT tenant_id, owner_id, output_json FROM workflow_runs
               WHERE kind = 'meeting.outputs.clear' AND state = 'succeeded'"""
        )
        rows = await cur.fetchall()
        await cur.close()
        cur = await conn.execute("SELECT tenant_id, owner_id, artifact_id FROM artifacts")
        protected_artifact_ids = {
            (str(row[0]), str(row[1]), str(row[2])) for row in await cur.fetchall()
        }
        await cur.close()
    for tenant_id, owner_id, raw_output in rows:
        try:
            output = json.loads(str(raw_output))
        except json.JSONDecodeError:
            continue
        ids = output.get("file_cleanup_artifact_ids") if isinstance(output, dict) else None
        if not isinstance(ids, list):
            continue
        artifact_ids.update(
            (str(tenant_id), str(owner_id), artifact_id)
            for artifact_id in ids
            if isinstance(artifact_id, str) and _ARTIFACT_ID_RE.fullmatch(artifact_id)
        )

    removed = 0
    for tenant_id, owner_id, artifact_id in artifact_ids - protected_artifact_ids:
        scope_root = scoped_directory_for(root, tenant_id, owner_id).resolve()
        candidates = [scope_root / artifact_id]
        if (tenant_id, owner_id) == (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
            candidates.append(root / artifact_id)
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            directory = candidate.resolve()
            if directory.parent != candidate.parent.resolve() or not directory.is_dir():
                continue
            shutil.rmtree(directory)
            removed += 1
            break
    return removed
