"""HTTP schemas for the owner-scoped layered memory API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.memory.models import MemoryKind


class MemoryRecallRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    conversation_id: str = Field(default="default", min_length=1, max_length=128)
    limit: int | None = Field(default=None, ge=1, le=20)


class MemoryExplicitExtractRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    source_id: str | None = Field(default=None, max_length=128)


class MemoryNodeUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2_000)
    kind: MemoryKind | None = None
    canonical_key: str | None = Field(default=None, min_length=1, max_length=256)
    salience: float | None = Field(default=None, ge=0.0, le=1.0)
    scope: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_change(self) -> MemoryNodeUpdateRequest:
        if all(
            value is None
            for value in (
                self.content,
                self.kind,
                self.canonical_key,
                self.salience,
                self.scope,
            )
        ):
            raise ValueError("at least one memory field must be supplied")
        return self


class MemoryProfileWriteRequest(BaseModel):
    value: Any
    description: str | None = Field(default=None, max_length=500)
    confirmation: Literal["user_explicit"] = "user_explicit"


class MemoryDeleteResponse(BaseModel):
    deleted: bool


class WorkingMemoryClearResponse(BaseModel):
    cleared: bool


__all__ = [
    "MemoryDeleteResponse",
    "MemoryExplicitExtractRequest",
    "MemoryNodeUpdateRequest",
    "MemoryProfileWriteRequest",
    "MemoryRecallRequest",
    "WorkingMemoryClearResponse",
]
