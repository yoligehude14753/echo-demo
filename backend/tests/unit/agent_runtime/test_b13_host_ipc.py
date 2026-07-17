"""B13 Python host IPC adapter gate."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.agent_capabilities import (
    GrantInput,
    PermissionRight,
    WorkspaceCapability,
    WorkspaceIdentity,
    freeze_grant,
)
from app.agent_capabilities.hosts import PathVerifier
from app.model_runtime import InMemoryCredentialResolver, ModelRuntimeConfigStore
from app.runtime.b13_host_ipc import B13HostAdapter
from app.runtime.b13_model_tool_provider import create_b13_provider_binding
from test_b13_model_tool_provider import _model_config
from yoli_llm import SSEFrame


async def _transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
    assert request.protocol == "openai_chat"
    assert resolver(request.credential_handle) == "host-ipc-secret"
    yield SSEFrame(
        data={
            "choices": [{"delta": {"role": "assistant", "content": "host"}, "finish_reason": None}]
        }
    )
    yield SSEFrame(data={"choices": [{"delta": {}, "finish_reason": "stop"}]})
    yield SSEFrame(done=True)


class _Session:
    def __init__(self) -> None:
        self.identity: Mapping[str, Any] | None = None
        self.events: list[tuple[str, Mapping[str, Any]]] = []
        self.checkpoints: list[Mapping[str, Any]] = []

    async def startup(self, kernel_build_identity: Mapping[str, Any]) -> Mapping[str, Any]:
        self.identity = kernel_build_identity
        return kernel_build_identity

    async def current_durable_event_seq(self) -> int:
        return len(self.events)

    async def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> str:
        self.checkpoints.append(checkpoint)
        return str(checkpoint["checkpointId"])

    async def close(self) -> None:
        return None

    async def append_durable_event(
        self, *, event_type: str, payload: Mapping[str, Any], occurred_at: str
    ) -> int:
        del occurred_at
        self.events.append((event_type, payload))
        return len(self.events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b13_host_ipc_binds_model_tool_receipt_and_session(tmp_path: Path) -> None:
    store = ModelRuntimeConfigStore(tmp_path / "config.json")
    store.save(_model_config())
    provider = create_b13_provider_binding(
        task_id="task-host-ipc",
        config_store=store,
        credential_resolver=InMemoryCredentialResolver({"credential://primary": "host-ipc-secret"}),
        transport=_transport,
    )
    note = tmp_path / "host.txt"
    note.write_text("host-ipc-value", encoding="utf-8")
    now = datetime(2026, 7, 16, tzinfo=UTC)
    grant = freeze_grant(
        GrantInput(
            grant_id="grant-host-ipc",
            revision=3,
            policy_revision=4,
            task_id="task-host-ipc",
            operation_key="op-host-ipc",
            workspace_identity=WorkspaceIdentity(
                workspace_id="ws-host-ipc", identity="workspace-host-ipc"
            ),
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
            workspace_roots=(
                WorkspaceCapability(
                    root_id="root-host-ipc",
                    canonical_path=str(tmp_path),
                    identity=PathVerifier.identity_for(tmp_path),
                    rights=(PermissionRight.READ,),
                ),
            ),
        )
    )
    session = _Session()
    adapter = B13HostAdapter(lambda task_id: provider, lambda _identity, _kernel: session)
    model = provider.snapshot.public_dict()
    await adapter.handle(
        "task-host-ipc",
        "op-host-ipc",
        "session.bind",
        {
            "taskId": "task-host-ipc",
            "operationKey": "op-host-ipc",
            "model": model,
            "grant": grant.model_dump(mode="json", by_alias=True),
            "kernelBuildIdentity": {"buildId": "b13-host-ipc"},
        },
    )
    await adapter.handle(
        "task-host-ipc",
        "op-host-ipc",
        "session.startup",
        {"kernelIdentity": {"buildId": "b13-host-ipc"}},
    )
    stream = await adapter.handle(
        "task-host-ipc",
        "op-host-ipc",
        "model.stream",
        {
            "request": {
                "requestId": "request-host-ipc",
                "taskId": "task-host-ipc",
                "operationKey": "op-host-ipc",
                "purpose": "agent_main",
                "configRevision": 7,
                "routeId": "main",
                "model": "model-redacted",
                "system": "system",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [],
                "maxOutputTokens": 32,
            }
        },
    )
    assert [event["type"] for event in stream["events"]] == [
        "message_start",
        "text_delta",
        "message_stop",
    ]
    tool = await adapter.handle(
        "task-host-ipc",
        "op-host-ipc",
        "tool.invoke",
        {
            "toolName": "path.read",
            "input": {"path": str(note), "rootId": "root-host-ipc"},
            "context": {
                "taskId": "task-host-ipc",
                "operationKey": "op-host-ipc",
                "grant": grant.model_dump(mode="json", by_alias=True),
                "requestId": "request-host-ipc",
                "toolUseId": "tool-host-ipc",
            },
        },
    )
    assert tool["result"] == "host-ipc-value"
    assert tool["receipt"]["toolUseId"] == "tool-host-ipc"
    assert tool["receipt"]["grantRevision"] == 3
    await adapter.handle(
        "task-host-ipc",
        "op-host-ipc",
        "events.publish",
        {
            "event": {
                "type": "agent.summary.updated",
                "occurredAt": "2026-07-16T00:00:00+00:00",
                "payload": {"summary": "host"},
            }
        },
    )
    assert session.events[0][0] == "agent.summary.updated"
