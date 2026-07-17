"""Task-scoped model revision pinning."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from app.model_runtime.config_store import ModelRuntimeConfigStore
from app.model_runtime.errors import (
    MODEL_CONFIG_INVALID,
    MODEL_CONFIG_STALE_REVISION,
    MODEL_TASK_ALREADY_PINNED,
    MODEL_TASK_NOT_PINNED,
    ModelRuntimeConfigError,
    ModelRuntimeStaleRevisionError,
)
from app.model_runtime.snapshot import ModelRuntimeSnapshot
from app.model_runtime.types import ModelPurpose, ModelRoute, ModelRuntimeConfig


@dataclass(frozen=True, slots=True, repr=False)
class TaskModelBinding:
    """Immutable task binding retained until the task is explicitly released."""

    task_id: str
    purpose: ModelPurpose
    revision: int
    config_hash: str
    snapshot: ModelRuntimeSnapshot
    routes: tuple[ModelRoute, ...]

    def __repr__(self) -> str:
        return (
            "TaskModelBinding("
            f"task_id={self.task_id!r}, purpose={self.purpose!r}, revision={self.revision!r}, "
            f"route_id={self.snapshot.route_id!r})"
        )

    def route(self, route_id: str) -> ModelRoute:
        route = next((item for item in self.routes if item.route_id == route_id), None)
        if route is None:
            raise ModelRuntimeConfigError(MODEL_CONFIG_INVALID, field="route_id")
        return route

    def snapshot_for_route(self, route_id: str) -> ModelRuntimeSnapshot:
        route = self.route(route_id)
        return ModelRuntimeSnapshot(
            schemaVersion=1,
            revision=self.revision,
            configHash=self.config_hash,
            purpose=self.purpose,
            routeId=route.route_id,
            protocol=route.protocol,
            model=route.model,
            capabilities=route.capabilities,
            limits=route.limits,
            tokenizer=route.tokenizer,
            reasoning=route.reasoning,
            credentialHandle=route.credential_handle,
        )


class TaskModelRevisionRegistry:
    """Pins one model config snapshot per task and exposes no mutable config."""

    def __init__(self, store: ModelRuntimeConfigStore) -> None:
        self._store = store
        self._bindings: dict[str, TaskModelBinding] = {}
        self._lock = RLock()

    def begin_task(
        self, task_id: str, purpose: ModelPurpose = "agent_main"
    ) -> ModelRuntimeSnapshot:
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ModelRuntimeConfigError(MODEL_TASK_NOT_PINNED, field="task_id")
        with self._lock:
            existing = self._bindings.get(normalized_task_id)
            if existing is not None:
                if existing.purpose != purpose:
                    raise ModelRuntimeConfigError(MODEL_TASK_ALREADY_PINNED, field="task_id")
                return existing.snapshot
            config = self._store.read()
            snapshot = _snapshot_for_purpose(config, purpose)
            binding = TaskModelBinding(
                task_id=normalized_task_id,
                purpose=purpose,
                revision=config.revision,
                config_hash=config.config_hash or snapshot.config_hash,
                snapshot=snapshot,
                routes=tuple(config.routes.values()),
            )
            self._bindings[normalized_task_id] = binding
            return snapshot

    def binding(self, task_id: str) -> TaskModelBinding:
        with self._lock:
            binding = self._bindings.get(task_id)
            if binding is None:
                raise ModelRuntimeConfigError(MODEL_TASK_NOT_PINNED, field="task_id")
            return binding

    def snapshot(self, task_id: str) -> ModelRuntimeSnapshot:
        return self.binding(task_id).snapshot

    def snapshot_for_route(self, task_id: str, route_id: str) -> ModelRuntimeSnapshot:
        return self.binding(task_id).snapshot_for_route(route_id)

    def assert_revision(self, task_id: str, revision: int) -> None:
        binding = self.binding(task_id)
        if binding.revision != revision:
            raise ModelRuntimeStaleRevisionError(
                MODEL_CONFIG_STALE_REVISION,
                field="revision",
            )

    def release_task(self, task_id: str) -> None:
        with self._lock:
            if task_id not in self._bindings:
                raise ModelRuntimeConfigError(MODEL_TASK_NOT_PINNED, field="task_id")
            del self._bindings[task_id]


def _snapshot_for_purpose(
    config: ModelRuntimeConfig, purpose: ModelPurpose
) -> ModelRuntimeSnapshot:
    # Keep the registry dependent on the public compiler API, not a duplicated
    # route/config interpretation.  The store returns ModelRuntimeConfig here.
    from app.model_runtime.config import compile_snapshot

    return compile_snapshot(config, purpose)


__all__ = ["TaskModelBinding", "TaskModelRevisionRegistry"]
