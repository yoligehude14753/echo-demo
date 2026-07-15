"""B13 production composition glue for the embedded agent runtime.

This module is the narrow seam between the B10 runtime adapter, the B11
SQLite persistence core, and the application-owned ``AgentTaskService``.  It
does not introduce another transport or persistence format.  The main
composition root can use :func:`create_b13_runtime_composition` and bind one
session port per ``OpenSessionInput`` identity.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.agents.embedded_runtime import (
    EmbeddedRuntimeBackend,
    RuntimeTransport,
)
from app.agents.service import AgentTaskService
from app.config import Settings
from app.runtime.artifact_skill_projection import ArtifactSkillProjection
from app.runtime.session_checkpoint_persistence import (
    JsonObject,
    PersistenceError,
    ResumeIdentity,
    SessionCheckpointRepository,
)
from app.workflows.service import WorkflowService

B13_RUNTIME_FD_ENV = "ECHODESK_RUNTIME_FD"


class B13CompositionError(RuntimeError):
    """Stable fail-closed errors raised while assembling the production seam."""

    def __init__(self, code: str, message: str = "B13 runtime composition rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class B13RuntimeRepositories:
    """Concrete repositories owned by the Echo backend composition root."""

    session_checkpoints: SessionCheckpointRepository
    artifact_skill_projection: ArtifactSkillProjection


@dataclass(slots=True)
class B13SessionCheckpointPort:
    """Identity-bound session port backed by the B11 SQLite repository.

    ``close`` releases a worker session as ``paused`` so a later worker
    restart can resume the same durable identity.  The application may call
    ``finalize`` when the durable session is genuinely terminal.
    """

    repository: SessionCheckpointRepository
    identity: ResumeIdentity

    @property
    def db_path(self) -> Path:
        return self.repository.db_path

    async def startup(self, kernel_build_identity: Mapping[str, Any]) -> Mapping[str, Any]:
        if dict(kernel_build_identity) != dict(self.identity.kernel_build_identity):
            raise PersistenceError(
                "RUNTIME_BUILD_MISMATCH",
                "runtime startup build identity does not match the session identity",
            )
        await self.repository.create_session(self.identity)
        state = await self._session_state()
        if state in {"closed", "stale"}:
            raise PersistenceError("SESSION_NOT_RESUMABLE", "session is not resumable")
        await self.repository.set_session_state(self.identity.session_id, "open")
        return dict(self.identity.kernel_build_identity)

    async def current_durable_event_seq(self) -> int:
        async with self._conn() as connection:
            cursor = await connection.execute(
                "SELECT last_durable_event_seq FROM agent_runtime_sessions WHERE session_id = ?",
                (self.identity.session_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
        return int(row[0])

    async def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> str:
        return await self.repository.save_checkpoint(self.identity.session_id, checkpoint)

    async def resume(
        self,
        *,
        current_durable_event_seq: int | None = None,
        now: str | None = None,
        max_age_seconds: int | None = None,
    ) -> JsonObject:
        durable_seq = (
            await self.current_durable_event_seq()
            if current_durable_event_seq is None
            else current_durable_event_seq
        )
        return await self.repository.resume(
            self.identity.session_id,
            self.identity,
            current_durable_event_seq=durable_seq,
            now=now,
            max_age_seconds=max_age_seconds,
        )

    async def restart(
        self,
        *,
        now: str | None = None,
        max_age_seconds: int | None = None,
    ) -> JsonObject:
        await self.startup(self.identity.kernel_build_identity)
        return await self.resume(now=now, max_age_seconds=max_age_seconds)

    async def close(self) -> None:
        await self.repository.set_session_state(self.identity.session_id, "paused")

    async def finalize(self) -> None:
        await self.repository.set_session_state(self.identity.session_id, "closed")

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.repository.db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(connection)
            yield connection

    async def _session_state(self) -> str:
        async with self._conn() as connection:
            cursor = await connection.execute(
                "SELECT state FROM agent_runtime_sessions WHERE session_id = ?",
                (self.identity.session_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
        return str(row[0])


@dataclass(slots=True)
class B13RuntimeComposition:
    """Concrete production dependencies exposed to the main composition root."""

    service: AgentTaskService
    backend: EmbeddedRuntimeBackend
    repositories: B13RuntimeRepositories

    def bind_session(self, identity: ResumeIdentity) -> B13SessionCheckpointPort:
        return B13SessionCheckpointPort(self.repositories.session_checkpoints, identity)


async def create_b13_runtime_composition(
    settings: Settings,
    event_bus: InMemoryEventBus,
    *,
    workflow: WorkflowService | None = None,
    holder_id: str | None = None,
    transport: RuntimeTransport | None = None,
) -> B13RuntimeComposition:
    """Build the concrete B13 runtime composition.

    The existing service constructor is retained for accepted-lineage
    compatibility.  B13 replaces only its runner instance with the explicit
    inherited-FD backend; no HTTP, CLI, executable, or global credential
    fallback is introduced.
    """

    if not str(settings.db_path):
        raise B13CompositionError("PERSISTENCE_DB_UNBOUND", "B13 persistence database path is required")
    if transport is None and not os.environ.get(B13_RUNTIME_FD_ENV):
        raise B13CompositionError(
            "EMBEDDED_RUNTIME_UNAVAILABLE",
            f"{B13_RUNTIME_FD_ENV} is required for the production runtime",
        )

    if transport is None:
        # B13 owns the production runtime choice explicitly.  The historical
        # service constructor is compatibility glue only and must not select a
        # configured HTTP backend for this path.
        backend = EmbeddedRuntimeBackend.from_environment()
    else:
        backend = EmbeddedRuntimeBackend(transport)
    if not backend.enabled:
        raise B13CompositionError(
            "EMBEDDED_RUNTIME_UNAVAILABLE",
            "inherited embedded runtime transport is unavailable",
        )

    service = AgentTaskService(
        settings,
        event_bus,
        workflow=workflow,
        holder_id=holder_id,
    )
    original_backend = service.backend
    service.backend = backend
    if original_backend is not backend and isinstance(original_backend, EmbeddedRuntimeBackend):
        await original_backend.aclose()

    repositories = B13RuntimeRepositories(
        session_checkpoints=SessionCheckpointRepository(settings.db_path),
        artifact_skill_projection=ArtifactSkillProjection(settings),
    )
    service.start_recovery_loop()
    return B13RuntimeComposition(
        service=service,
        backend=backend,
        repositories=repositories,
    )


__all__ = [
    "B13_RUNTIME_FD_ENV",
    "B13CompositionError",
    "B13RuntimeComposition",
    "B13RuntimeRepositories",
    "B13SessionCheckpointPort",
    "create_b13_runtime_composition",
]
