"""Deterministic primitives for the AgentTask durable event state machine.

The runtime event identity is deliberately separate from Echo's durable
sequence.  A worker may replay the same event, while the backend must assign
that event at most one durable sequence.  This module contains the pure
admission rules; the AgentTask repository remains responsible for persisting
the returned decision atomically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "cancel_failed", "timeout"}
)

_VOLATILE_ENVELOPE_FIELDS = frozenset({"occurredAt", "receivedAt", "ts"})
_REQUIRED_ENVELOPE_FIELDS = (
    "schemaVersion",
    "taskId",
    "operationKey",
    "runtimeEventId",
    "type",
    "payload",
)


def _canonical_envelope(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable raw-event identity material.

    Runtime timestamps describe observation time, not event identity.  They
    are therefore excluded so reconnect/replay cannot create a second Echo
    event merely because the frame was observed at a different time.
    """

    missing = [field for field in _REQUIRED_ENVELOPE_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"runtime event missing required fields: {', '.join(missing)}")
    return {
        key: raw[key]
        for key in _REQUIRED_ENVELOPE_FIELDS
        if key not in _VOLATILE_ENVELOPE_FIELDS
    }


def raw_event_hash(raw: Mapping[str, Any]) -> str:
    """Hash one runtime event without conflating it with durable ``seq``."""

    body = json.dumps(
        _canonical_envelope(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class DurableEventAdmission:
    """The result the repository should apply in one SQLite transaction."""

    raw_hash: str
    durable_seq: int
    duplicate: bool
    audit_only: bool
    effective_state: str


class DurableEventStateMachine:
    """Model raw-hash dedupe, durable sequence allocation, and terminal arbitration.

    ``known_raw_events`` is a hash-to-seq view loaded from the repository.  It
    is mutated only as an in-memory admission view; callers still need to
    persist the accepted event and task snapshot atomically.
    """

    def __init__(
        self,
        *,
        last_seq: int,
        current_state: str,
        known_raw_events: MutableMapping[str, int] | None = None,
    ) -> None:
        if last_seq < 0:
            raise ValueError("last_seq must be non-negative")
        self.last_seq = last_seq
        self.current_state = current_state
        self.known_raw_events = known_raw_events if known_raw_events is not None else {}

    def admit(self, raw_event: Mapping[str, Any], *, incoming_state: str) -> DurableEventAdmission:
        """Admit one raw event, returning its existing/new durable sequence.

        Once a terminal state has been accepted, every later state-bearing
        event is retained only as audit data.  This includes a distinct event
        that repeats the same terminal state: first-terminal-wins is about the
        first durable terminal decision, not merely about unequal state names.
        """

        if not incoming_state:
            raise ValueError("incoming_state must be non-empty")
        digest = raw_event_hash(raw_event)
        existing_seq = self.known_raw_events.get(digest)
        if existing_seq is not None:
            return DurableEventAdmission(
                raw_hash=digest,
                durable_seq=existing_seq,
                duplicate=True,
                audit_only=False,
                effective_state=self.current_state,
            )

        durable_seq = self.last_seq + 1
        self.last_seq = durable_seq
        self.known_raw_events[digest] = durable_seq

        audit_only = self.current_state in TERMINAL_STATES
        if not audit_only:
            self.current_state = incoming_state

        return DurableEventAdmission(
            raw_hash=digest,
            durable_seq=durable_seq,
            duplicate=False,
            audit_only=audit_only,
            effective_state=self.current_state,
        )


__all__ = [
    "TERMINAL_STATES",
    "DurableEventAdmission",
    "DurableEventStateMachine",
    "raw_event_hash",
]
