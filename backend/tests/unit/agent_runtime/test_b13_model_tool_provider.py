"""B13 focused source binding and controlled tool receipt coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from app.runtime.b13_model_tool_provider import (
    B13_YOLI_TRANSPORT_SHA,
    bind_b06p_tool_hosts,
    create_b13_provider_binding,
    make_b13_file_read_invocation,
    make_b13_model_request,
)
from yoli_llm import SSEFrame


def _model_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": 7,
        "activated_at": datetime(2026, 7, 16, tzinfo=UTC),
        "routes": {
            "agent_main": {
                "route_id": "main",
                "protocol": "openai_chat",
                "base_url": "https://provider.example/v1",
                "credential_handle": "credential://primary",
                "model": "model-redacted",
                "fallback_route_ids": [],
                "capabilities": {
                    "streaming": True,
                    "tool_use": True,
                    "parallel_tool_use": True,
                    "tool_choice": True,
                    "system_messages": True,
                    "usage_in_stream": True,
                    "prompt_cache": False,
                    "multimodal_images": False,
                    "multimodal_documents": False,
                },
                "limits": {
                    "context_window": 128_000,
                    "max_output_tokens": 8_000,
                    "request_timeout_s": 30.0,
                    "max_retries": 0,
                },
                "tokenizer": {
                    "kind": "conservative_estimate",
                    "identifier": "b13-test-tokenizer",
                    "estimated": True,
                    "safety_margin_tokens": 16,
                },
                "reasoning": {
                    "mode": "none",
                    "strip_think_tags": True,
                    "token_budget": None,
                },
            }
        },
    }


async def _transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
    assert request.protocol == "openai_chat"
    assert resolver(request.credential_handle) == "provider-secret-held-at-transport"
    yield SSEFrame(data={"choices": [{"delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]})
    yield SSEFrame(data={"choices": [{"delta": {}, "finish_reason": "stop"}]})
    yield SSEFrame(done=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b13_binds_b05m_snapshot_and_runs_one_provider_turn(tmp_path: Path) -> None:
    store = ModelRuntimeConfigStore(tmp_path / "config.json")
    store.save(_model_config())
    binding = create_b13_provider_binding(
        task_id="task-b13",
        config_store=store,
        credential_resolver=InMemoryCredentialResolver(
            {"credential://primary": "provider-secret-held-at-transport"}
        ),
        transport=_transport,
    )

    result = [event async for event in binding.model_gateway.stream(make_b13_model_request(binding))]

    assert binding.transport_sha == B13_YOLI_TRANSPORT_SHA
    assert binding.snapshot.revision == 7
    assert binding.snapshot.route_id == "main"
    assert binding.snapshot.public_dict().get("credentialHandle") is None
    assert [event.type for event in result] == ["message_start", "text_delta", "message_stop"]
    assert all("provider-secret-held-at-transport" not in str(event.as_dict()) for event in result)


@pytest.mark.unit
def test_b13_binds_real_b06p_file_host_and_emits_receipt(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("b13-tool-value", encoding="utf-8")
    root_identity = PathVerifier.identity_for(tmp_path)
    now = datetime(2026, 7, 16, tzinfo=UTC)
    grant = freeze_grant(
        GrantInput(
            grant_id="grant-b13",
            revision=3,
            policy_revision=4,
            task_id="task-b13",
            operation_key="op-b13",
            workspace_identity=WorkspaceIdentity(workspace_id="ws-b13", identity="workspace-b13"),
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
            workspace_roots=(
                WorkspaceCapability(
                    root_id="root-b13",
                    canonical_path=str(tmp_path),
                    identity=root_identity,
                    rights=(PermissionRight.READ,),
                ),
            ),
        )
    )
    invocation = make_b13_file_read_invocation(
        grant=grant,
        path=str(note),
        root_id="root-b13",
    )

    outcome = bind_b06p_tool_hosts().invoke("path.read", invocation)

    assert outcome.ok
    assert outcome.value == b"b13-tool-value"
    assert outcome.receipt.result == "succeeded"
    assert outcome.receipt.tool_use_id == "b13-tool-1"


@pytest.mark.unit
def test_b13_missing_model_config_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(Exception) as error:
        create_b13_provider_binding(
            task_id="task-b13",
            config_store=ModelRuntimeConfigStore(tmp_path / "missing.json"),
            credential_resolver=InMemoryCredentialResolver(),
        )
    assert "SECRET" not in str(error.value).upper()
