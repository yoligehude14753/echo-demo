"""B13 host-side IPC adapter for the embedded Electron worker.

The worker receives only value envelopes.  This module keeps the concrete
B05M gateway, B06P registry, credential resolver, grant authority, and B11
session port on the Python host side of the inherited-FD transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.agent_capabilities.types import DenyCode
from app.runtime.b13_model_tool_provider import (
    B13ProviderBinding,
    make_b13_file_read_invocation,
)

B13_HOST_PROTOCOL_VERSION: Final = 1
B13_HOST_RESPONSE_TYPE: Final = "b13.host.response"
SUPPORTED_DURABLE_HOST_EVENTS: Final = frozenset(
    {
        "agent.summary.updated",
        "agent.compaction.started",
        "agent.compaction.completed",
        "agent.compaction.failed",
        "agent.brief",
    }
)


class B13HostBindingError(RuntimeError):
    def __init__(self, code: str, message: str = "B13 host binding rejected") -> None:
        super().__init__(message)
        self.code = code


class B13SessionPort(Protocol):
    async def startup(self, kernel_build_identity: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def current_durable_event_seq(self) -> int: ...
    async def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> str: ...
    async def close(self) -> None: ...
    async def append_durable_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: str,
    ) -> int: ...


ProviderFactory = Callable[[str], B13ProviderBinding]
SessionFactory = Callable[[Mapping[str, Any], Mapping[str, Any]], B13SessionPort]


@dataclass(slots=True)
class _Binding:
    task_id: str
    operation_key: str
    provider: B13ProviderBinding
    session: B13SessionPort
    grant: Any
    kernel_identity: Mapping[str, Any]


def _obj(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B13HostBindingError("B13_HOST_SCHEMA_INVALID", f"{label} must be an object")
    return {str(key): child for key, child in value.items()}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise B13HostBindingError("B13_HOST_SCHEMA_INVALID", f"{label} is required")
    return value


def _same_model_binding(snapshot: Any, public_model: Mapping[str, Any]) -> bool:
    return snapshot.public_dict() == dict(public_model)


def _safe_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _model_request(payload: Mapping[str, Any]) -> Any:
    """Convert the worker's public camelCase request to the B05M value shape."""

    from app.services.model_gateway import AgentModelRequest

    raw = _obj(payload.get("request"), "request")
    names = {
        "requestId": "request_id",
        "taskId": "task_id",
        "operationKey": "operation_key",
        "configRevision": "config_revision",
        "routeId": "route_id",
        "toolChoice": "tool_choice",
        "maxOutputTokens": "max_output_tokens",
        "stopSequences": "stop_sequences",
    }
    normalized = {names.get(key, key): value for key, value in raw.items()}
    return AgentModelRequest(**normalized)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "path.read",
            "description": "Read a file through the B06P verified file host.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "rootId": {"type": "string"},
                },
                "required": ["path", "rootId"],
                "additionalProperties": False,
            },
            "traits": {
                "readOnly": True,
                "destructive": False,
                "concurrencySafe": True,
                "capability": "path.read",
            },
        }
    ]


class B13HostAdapter:
    """Dispatch allowlisted worker methods to concrete Python authorities."""

    def __init__(self, provider_factory: ProviderFactory, session_factory: SessionFactory) -> None:
        self._provider_factory = provider_factory
        self._session_factory = session_factory
        self._bindings: dict[tuple[str, str], _Binding] = {}

    async def handle(  # noqa: PLR0911, PLR0912
        self,
        task_id: str,
        operation_key: str,
        method: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        key = (task_id, operation_key)
        if method == "session.bind":
            return await self._bind(key, payload)
        binding = self._bindings.get(key)
        if binding is None:
            raise B13HostBindingError("B13_HOST_BINDING_UNBOUND")
        if method == "session.startup":
            identity = _obj(payload.get("kernelIdentity"), "kernelIdentity")
            result = await binding.session.startup(identity)
            return {"kernelIdentity": dict(result)}
        if method == "session.current_durable_event_seq":
            return {"durableEventSeq": await binding.session.current_durable_event_seq()}
        if method == "session.save_checkpoint":
            checkpoint = _obj(payload.get("checkpoint"), "checkpoint")
            await binding.session.save_checkpoint(checkpoint)
            return {}
        if method == "session.close":
            await binding.session.close()
            self._bindings.pop(key, None)
            return {}
        if method == "model.count_tokens":
            return await self._count_tokens(binding, payload)
        if method == "model.stream":
            return await self._stream_model(binding, payload)
        if method == "tools.list":
            return {"tools": _tool_definitions()}
        if method == "tool.describe":
            return {"description": "Read a file through the B06P verified file host."}
        if method == "tool.validate":
            return self._validate_tool(binding, payload)
        if method == "tool.invoke":
            return self._invoke_tool(binding, payload)
        if method == "events.publish":
            return await self._publish_event(binding, payload)
        if method in {"events.audit", "telemetry.record"}:
            return {}
        raise B13HostBindingError("B13_HOST_METHOD_UNSUPPORTED", method)

    async def _bind(self, key: tuple[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id, operation_key = key
        public_open = _obj(payload, "open") if "open" in payload else dict(payload)
        if _string(public_open.get("taskId"), "taskId") != task_id or _string(public_open.get("operationKey"), "operationKey") != operation_key:
            raise B13HostBindingError("B13_HOST_IDENTITY_MISMATCH")
        public_model = _obj(public_open.get("model"), "model")
        public_grant = _obj(public_open.get("grant"), "grant")
        kernel_identity = _obj(payload.get("kernelBuildIdentity"), "kernelBuildIdentity")
        provider = self._provider_factory(task_id)
        if provider.task_id != task_id or not _same_model_binding(provider.snapshot, public_model):
            raise B13HostBindingError("MODEL_BINDING_MISMATCH")
        from app.agent_capabilities.types import GrantSnapshot

        grant = GrantSnapshot.model_validate({**public_grant, "operation_key": operation_key})
        session = self._session_factory(
            {
                "taskId": task_id,
                "operationKey": operation_key,
                "modelConfigRevision": public_model.get("revision"),
                "grantSnapshot": grant.model_dump(mode="json", by_alias=True),
                "kernelBuildIdentity": kernel_identity,
            },
            kernel_identity,
        )
        self._bindings[key] = _Binding(task_id, operation_key, provider, session, grant, kernel_identity)
        return {"tools": _tool_definitions()}

    async def _count_tokens(self, binding: _Binding, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _model_request(payload)
        result = await binding.provider.model_gateway.count_tokens(request)
        return {"inputTokens": result.tokens, "estimated": result.estimated}

    async def _stream_model(self, binding: _Binding, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _model_request(payload)
        events: list[dict[str, Any]] = []
        async for event in binding.provider.model_gateway.stream(request):
            events.append(event.as_dict())
        return {"events": events}

    def _context(self, binding: _Binding, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        context = _obj(payload.get("context"), "context")
        if _string(context.get("taskId"), "context.taskId") != binding.task_id or _string(context.get("operationKey"), "context.operationKey") != binding.operation_key:
            raise B13HostBindingError("B13_HOST_IDENTITY_MISMATCH")
        tool_name = _string(payload.get("toolName"), "toolName")
        tool_input = _obj(payload.get("input"), "input")
        return context, tool_input, tool_name, _string(context.get("toolUseId"), "toolUseId")

    def _validate_tool(self, binding: _Binding, payload: Mapping[str, Any]) -> dict[str, Any]:
        context, _tool_input, tool_name, _tool_use_id = self._context(binding, payload)
        if tool_name != "path.read":
            return {"allowed": False, "reasonCode": DenyCode.TOOL_NOT_REGISTERED.value, "message": "tool is not registered"}
        grant = _obj(context.get("grant"), "context.grant")
        if (
            grant.get("taskId", grant.get("task_id")) != binding.task_id
            or grant.get("operationKey", grant.get("operation_key", binding.operation_key)) != binding.operation_key
            or grant.get("revision") != binding.grant.revision
        ):
            return {"allowed": False, "reasonCode": DenyCode.GRANT_BINDING_MISMATCH.value, "message": "grant binding mismatch"}
        return {"allowed": True}

    def _invoke_tool(self, binding: _Binding, payload: Mapping[str, Any]) -> dict[str, Any]:
        context, tool_input, tool_name, tool_use_id = self._context(binding, payload)
        if tool_name != "path.read":
            raise B13HostBindingError("TOOL_NOT_REGISTERED")
        _string(context.get("requestId"), "requestId")
        invocation = make_b13_file_read_invocation(
            grant=binding.grant,
            path=_string(tool_input.get("path"), "path"),
            root_id=_string(tool_input.get("rootId"), "rootId"),
            tool_use_id=tool_use_id,
        )
        outcome = binding.provider.tool_hosts.invoke(tool_name, invocation)
        receipt = outcome.receipt.model_dump(mode="json", by_alias=True)
        value = _safe_value(outcome.value) if outcome.value is not None else ""
        return {
            "value": value,
            "result": value,
            "isError": not outcome.ok,
            "receipt": receipt,
        }

    async def _publish_event(self, binding: _Binding, payload: Mapping[str, Any]) -> dict[str, Any]:
        event = _obj(payload.get("event"), "event")
        event_type = _string(event.get("type"), "event.type")
        if event_type not in SUPPORTED_DURABLE_HOST_EVENTS:
            return {"durableEventSeq": await binding.session.current_durable_event_seq()}
        seq = await binding.session.append_durable_event(
            event_type=event_type,
            payload=_obj(event.get("payload"), "event.payload"),
            occurred_at=_string(event.get("occurredAt"), "event.occurredAt"),
        )
        return {"durableEventSeq": seq}


__all__ = ["B13HostAdapter", "B13HostBindingError", "ProviderFactory", "SessionFactory"]
