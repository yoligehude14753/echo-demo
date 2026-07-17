"""Durable projections for context/artifact links and B06P skill receipts.

This module is deliberately an adapter around existing authority sources:
``GeneratedArtifact`` remains the artifact fact source and ``SkillReceipt``
remains the B06P receipt/provenance source.  The projection stores only
task-bound identifiers, hashes, and redacted metadata; it never stores skill
payloads, artifact filesystem paths, or credential material.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite
from pydantic import Field, field_validator, model_validator

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.agent_capabilities.redaction import redact_text
from app.agent_capabilities.skill_host import SkillReceipt
from app.agent_capabilities.types import FrozenModel
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.security.context import current_principal

PROJECTION_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_SAFE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProjectionIntegrityError(ValueError):
    """Raised when a durable projection would be ambiguous or unsafe."""


def _safe_text(value: str, field_name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ProjectionIntegrityError(f"{field_name} must be a non-empty bounded string")
    if any(char in value for char in "\x00\r\n"):
        raise ProjectionIntegrityError(f"{field_name} contains a control character")
    if redact_text(value) != value:
        raise ProjectionIntegrityError(f"{field_name} contains redacted material")
    return value


def _digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProjectionIntegrityError(f"{field_name} must be a SHA-256 digest")
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionIntegrityError("timestamp must include timezone")
    return value.astimezone(UTC)


class ArtifactContextMapping(FrozenModel):
    """One durable, task-bound link from a context reference to an artifact."""

    schema_version: Literal[1] = 1
    mapping_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    checkpoint_id: str | None = Field(default=None, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    context_ref: str = Field(min_length=1, max_length=512)
    relation: Literal["input", "output", "derived"] = "output"
    artifact_sha256: str | None = None
    created_at: datetime
    mapping_sha256: str = Field(min_length=71, max_length=71)

    @field_validator("mapping_id", "task_id", "operation_key", "artifact_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _safe_text(value, "identifier", max_length=256)

    @field_validator("checkpoint_id")
    @classmethod
    def _checkpoint_id(cls, value: str | None) -> str | None:
        return None if value is None else _safe_text(value, "checkpoint_id", max_length=256)

    @field_validator("context_ref")
    @classmethod
    def _context_ref(cls, value: str) -> str:
        return _safe_text(value, "context_ref")

    @field_validator("artifact_sha256")
    @classmethod
    def _artifact_hash(cls, value: str | None) -> str | None:
        return _digest(value, "artifact_sha256")

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("mapping_sha256")
    @classmethod
    def _mapping_hash(cls, value: str) -> str:
        if not _SAFE_DIGEST_RE.fullmatch(value):
            raise ProjectionIntegrityError("mapping_sha256 must be a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _verify_mapping_hash(self) -> ArtifactContextMapping:
        body = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "operation_key": self.operation_key,
            "checkpoint_id": self.checkpoint_id,
            "artifact_id": self.artifact_id,
            "context_ref": self.context_ref,
            "relation": self.relation,
            "artifact_sha256": self.artifact_sha256,
        }
        if _sha256(body) != self.mapping_sha256:
            raise ProjectionIntegrityError("mapping digest does not match projection content")
        return self

    @classmethod
    def for_context_ref(
        cls,
        *,
        task_id: str,
        operation_key: str,
        artifact: GeneratedArtifact,
        context_ref: str,
        checkpoint_id: str | None = None,
        relation: Literal["input", "output", "derived"] = "output",
        created_at: datetime | None = None,
    ) -> ArtifactContextMapping:
        schema_version: Literal[1] = 1
        safe_task_id = _safe_text(task_id, "task_id", max_length=256)
        safe_operation_key = _safe_text(operation_key, "operation_key", max_length=256)
        safe_checkpoint_id = (
            None
            if checkpoint_id is None
            else _safe_text(checkpoint_id, "checkpoint_id", max_length=256)
        )
        safe_artifact_id = _safe_text(artifact.artifact_id, "artifact_id", max_length=256)
        safe_context_ref = _safe_text(context_ref, "context_ref")
        artifact_sha256 = _digest(artifact.metadata.get("sha256"), "artifact sha256")
        body = {
            "schema_version": schema_version,
            "task_id": safe_task_id,
            "operation_key": safe_operation_key,
            "checkpoint_id": safe_checkpoint_id,
            "artifact_id": safe_artifact_id,
            "context_ref": safe_context_ref,
            "relation": relation,
            "artifact_sha256": artifact_sha256,
        }
        return cls(
            schema_version=schema_version,
            task_id=safe_task_id,
            operation_key=safe_operation_key,
            checkpoint_id=safe_checkpoint_id,
            artifact_id=safe_artifact_id,
            context_ref=safe_context_ref,
            relation=relation,
            artifact_sha256=artifact_sha256,
            mapping_id=f"artifact-context-{hashlib.sha256(_canonical_json(body)).hexdigest()[:32]}",
            created_at=_utc(created_at or datetime.now(UTC)),
            mapping_sha256=_sha256(body),
        )


class SkillReceiptProjection(FrozenModel):
    """Durable, value-free projection of one B06P ``SkillReceipt``."""

    schema_version: Literal[1] = 1
    receipt_id: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    outcome: str = Field(min_length=1, max_length=32)
    result: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)
    tool_use_id: str = Field(min_length=1, max_length=256)
    grant_id: str | None = Field(default=None, max_length=256)
    grant_revision: int | None = Field(default=None, ge=1)
    policy_revision: int | None = Field(default=None, ge=1)
    skill_identity: str = Field(min_length=1, max_length=256)
    skill_version: str = Field(min_length=1, max_length=128)
    manifest_sha256: str = Field(min_length=71, max_length=71)
    resource_hashes: tuple[str, ...] = ()
    provenance: str = Field(min_length=1, max_length=256)
    signer_id: str = Field(min_length=1, max_length=256)
    input_sha256: str | None = None
    output_sha256: str | None = None
    redacted: Literal[True] = True
    projection_sha256: str = Field(min_length=71, max_length=71)

    @field_validator(
        "receipt_id",
        "task_id",
        "operation_key",
        "tool_use_id",
        "skill_identity",
        "skill_version",
        "provenance",
        "signer_id",
    )
    @classmethod
    def _safe_fields(cls, value: str) -> str:
        return _safe_text(value, "receipt field", max_length=256)

    @field_validator("grant_id")
    @classmethod
    def _grant_id(cls, value: str | None) -> str | None:
        return None if value is None else _safe_text(value, "grant_id", max_length=256)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("manifest_sha256", "input_sha256", "output_sha256")
    @classmethod
    def _hashes(cls, value: str | None) -> str | None:
        return _digest(value, "receipt hash")

    @field_validator("resource_hashes")
    @classmethod
    def _resource_hashes(cls, value: Iterable[str]) -> tuple[str, ...]:
        return tuple(_digest(item, "resource hash") or "" for item in value)

    @field_validator("projection_sha256")
    @classmethod
    def _projection_hash(cls, value: str) -> str:
        if not _SAFE_DIGEST_RE.fullmatch(value):
            raise ProjectionIntegrityError("projection_sha256 must be a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _verify_projection_hash(self) -> SkillReceiptProjection:
        digest_body = self.model_dump(mode="json", exclude={"projection_sha256"})
        if _sha256(digest_body) != self.projection_sha256:
            raise ProjectionIntegrityError("receipt digest does not match projection content")
        return self

    @classmethod
    def from_receipt(cls, receipt: SkillReceipt) -> SkillReceiptProjection:
        if receipt.redacted is not True:
            raise ProjectionIntegrityError("only redacted skill receipts are durable")
        body = receipt.model_dump(mode="json")
        body.pop("redacted", None)
        body.pop("schema_version", None)
        body.pop("event_type", None)
        body.pop("operation", None)
        occurred_at = body["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        body["occurred_at"] = _utc(occurred_at)
        body["manifest_sha256"] = _digest(body["manifest_sha256"], "manifest_sha256")
        body["resource_hashes"] = tuple(
            _digest(item, "resource hash") for item in body["resource_hashes"]
        )
        body["input_sha256"] = _digest(body["input_sha256"], "input_sha256")
        body["output_sha256"] = _digest(body["output_sha256"], "output_sha256")
        projection = cls.model_construct(
            schema_version=PROJECTION_SCHEMA_VERSION,
            **body,
            projection_sha256="sha256:" + "0" * 64,
        )
        digest_body = projection.model_dump(mode="json", exclude={"projection_sha256"})
        return projection.model_copy(update={"projection_sha256": _sha256(digest_body)})


def mappings_for_artifact(
    *,
    task_id: str,
    operation_key: str,
    artifact: GeneratedArtifact,
    context_refs: Sequence[str],
    checkpoint_id: str | None = None,
    relation: Literal["input", "output", "derived"] = "output",
    created_at: datetime | None = None,
) -> tuple[ArtifactContextMapping, ...]:
    """Build deterministic mappings without touching the artifact repository."""

    return tuple(
        ArtifactContextMapping.for_context_ref(
            task_id=task_id,
            operation_key=operation_key,
            artifact=artifact,
            context_ref=context_ref,
            checkpoint_id=checkpoint_id,
            relation=relation,
            created_at=created_at,
        )
        for context_ref in dict.fromkeys(context_refs)
    )


def _scope() -> tuple[str, str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


def _receipt_json(receipt: SkillReceiptProjection) -> str:
    return json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


class ArtifactSkillProjection:
    """Owner-scoped durable adapter with caller-owned transaction methods."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.settings.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = aiosqlite.Row
            yield conn

    async def project(
        self,
        *,
        mappings: Sequence[ArtifactContextMapping] = (),
        receipts: Sequence[SkillReceipt | SkillReceiptProjection] = (),
    ) -> tuple[tuple[ArtifactContextMapping, ...], tuple[SkillReceiptProjection, ...]]:
        """Persist mappings and receipts atomically and idempotently."""

        projections = tuple(
            receipt
            if isinstance(receipt, SkillReceiptProjection)
            else SkillReceiptProjection.from_receipt(receipt)
            for receipt in receipts
        )
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            for mapping in mappings:
                await self.save_mapping_tx(conn, mapping)
            for receipt in projections:
                await self.save_receipt_tx(conn, receipt)
            await conn.commit()
        return tuple(mappings), projections

    async def save_mapping_tx(
        self,
        conn: aiosqlite.Connection,
        mapping: ArtifactContextMapping,
    ) -> None:
        """Persist one mapping inside a transaction owned by the caller."""

        tenant_id, _device_id, owner_id = _scope()
        cur = await conn.execute(
            """SELECT 1 FROM artifacts
               WHERE tenant_id = ? AND owner_id = ? AND artifact_id = ?""",
            (tenant_id, owner_id, mapping.artifact_id),
        )
        exists = await cur.fetchone()
        await cur.close()
        if exists is None:
            raise ProjectionIntegrityError("artifact must be durable before mapping")
        await conn.execute(
            """INSERT INTO agent_artifact_context_projections
               (tenant_id, owner_id, mapping_id, schema_version, task_id,
                operation_key, checkpoint_id, artifact_id, context_ref, relation,
                artifact_sha256, created_at, mapping_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, owner_id, mapping_id) DO NOTHING""",
            (
                tenant_id,
                owner_id,
                mapping.mapping_id,
                mapping.schema_version,
                mapping.task_id,
                mapping.operation_key,
                mapping.checkpoint_id,
                mapping.artifact_id,
                mapping.context_ref,
                mapping.relation,
                mapping.artifact_sha256,
                mapping.created_at.isoformat(),
                mapping.mapping_sha256,
            ),
        )
        await self._assert_mapping_digest(conn, tenant_id, owner_id, mapping)

    async def save_receipt_tx(
        self,
        conn: aiosqlite.Connection,
        receipt: SkillReceiptProjection,
    ) -> None:
        """Persist one B06P receipt projection inside a caller transaction."""

        tenant_id, _device_id, owner_id = _scope()
        await conn.execute(
            """INSERT INTO agent_skill_receipt_projections
               (tenant_id, owner_id, receipt_id, schema_version, occurred_at,
                outcome, result, code, capability, task_id, operation_key,
                tool_use_id, grant_id, grant_revision, policy_revision,
                skill_identity, skill_version, manifest_sha256, resource_hashes_json,
                provenance, signer_id, input_sha256, output_sha256, redacted,
                projection_sha256, projection_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, owner_id, receipt_id) DO NOTHING""",
            (
                tenant_id,
                owner_id,
                receipt.receipt_id,
                receipt.schema_version,
                receipt.occurred_at.isoformat(),
                receipt.outcome,
                receipt.result,
                receipt.code,
                receipt.capability,
                receipt.task_id,
                receipt.operation_key,
                receipt.tool_use_id,
                receipt.grant_id,
                receipt.grant_revision,
                receipt.policy_revision,
                receipt.skill_identity,
                receipt.skill_version,
                receipt.manifest_sha256,
                json.dumps(receipt.resource_hashes, ensure_ascii=False),
                receipt.provenance,
                receipt.signer_id,
                receipt.input_sha256,
                receipt.output_sha256,
                1,
                receipt.projection_sha256,
                _receipt_json(receipt),
            ),
        )
        cur = await conn.execute(
            """SELECT projection_sha256 FROM agent_skill_receipt_projections
               WHERE tenant_id = ? AND owner_id = ? AND receipt_id = ?""",
            (tenant_id, owner_id, receipt.receipt_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None or row[0] != receipt.projection_sha256:
            raise ProjectionIntegrityError("receipt id is already bound to a different projection")

    async def _assert_mapping_digest(
        self,
        conn: aiosqlite.Connection,
        tenant_id: str,
        owner_id: str,
        mapping: ArtifactContextMapping,
    ) -> None:
        cur = await conn.execute(
            """SELECT mapping_sha256 FROM agent_artifact_context_projections
               WHERE tenant_id = ? AND owner_id = ? AND mapping_id = ?""",
            (tenant_id, owner_id, mapping.mapping_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None or row[0] != mapping.mapping_sha256:
            raise ProjectionIntegrityError("mapping id is already bound to a different projection")

    async def list_mappings(
        self,
        *,
        task_id: str | None = None,
        operation_key: str | None = None,
        artifact_id: str | None = None,
        limit: int = 200,
    ) -> list[ArtifactContextMapping]:
        tenant_id, _device_id, owner_id = _scope()
        clauses = ["tenant_id = ?", "owner_id = ?"]
        params: list[Any] = [tenant_id, owner_id]
        for column, value in (
            ("task_id", task_id),
            ("operation_key", operation_key),
            ("artifact_id", artifact_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(max(1, min(limit, 1000)))
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_artifact_context_projections WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC, mapping_id ASC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_mapping_from_row(row) for row in rows]

    async def list_receipts(
        self,
        *,
        task_id: str | None = None,
        operation_key: str | None = None,
        limit: int = 200,
    ) -> list[SkillReceiptProjection]:
        tenant_id, _device_id, owner_id = _scope()
        clauses = ["tenant_id = ?", "owner_id = ?"]
        params: list[Any] = [tenant_id, owner_id]
        for column, value in (("task_id", task_id), ("operation_key", operation_key)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(max(1, min(limit, 1000)))
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_skill_receipt_projections WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at ASC, receipt_id ASC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_receipt_from_row(row) for row in rows]


def _mapping_from_row(row: aiosqlite.Row) -> ArtifactContextMapping:
    return ArtifactContextMapping(
        schema_version=row["schema_version"],
        mapping_id=row["mapping_id"],
        task_id=row["task_id"],
        operation_key=row["operation_key"],
        checkpoint_id=row["checkpoint_id"],
        artifact_id=row["artifact_id"],
        context_ref=row["context_ref"],
        relation=row["relation"],
        artifact_sha256=row["artifact_sha256"],
        created_at=row["created_at"],
        mapping_sha256=row["mapping_sha256"],
    )


def _receipt_from_row(row: aiosqlite.Row) -> SkillReceiptProjection:
    return SkillReceiptProjection(
        schema_version=row["schema_version"],
        receipt_id=row["receipt_id"],
        occurred_at=row["occurred_at"],
        outcome=row["outcome"],
        result=row["result"],
        code=row["code"],
        capability=row["capability"],
        task_id=row["task_id"],
        operation_key=row["operation_key"],
        tool_use_id=row["tool_use_id"],
        grant_id=row["grant_id"],
        grant_revision=row["grant_revision"],
        policy_revision=row["policy_revision"],
        skill_identity=row["skill_identity"],
        skill_version=row["skill_version"],
        manifest_sha256=row["manifest_sha256"],
        resource_hashes=tuple(json.loads(row["resource_hashes_json"])),
        provenance=row["provenance"],
        signer_id=row["signer_id"],
        input_sha256=row["input_sha256"],
        output_sha256=row["output_sha256"],
        redacted=True,
        projection_sha256=row["projection_sha256"],
    )


_projection: ArtifactSkillProjection | None = None


def get_artifact_skill_projection(settings: Settings) -> ArtifactSkillProjection:
    global _projection  # noqa: PLW0603
    if _projection is None:
        _projection = ArtifactSkillProjection(settings)
    return _projection


def reset_artifact_skill_projection_for_test() -> None:
    global _projection  # noqa: PLW0603
    _projection = None


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ArtifactContextMapping",
    "ArtifactSkillProjection",
    "ProjectionIntegrityError",
    "SkillReceiptProjection",
    "get_artifact_skill_projection",
    "mappings_for_artifact",
    "reset_artifact_skill_projection_for_test",
]
