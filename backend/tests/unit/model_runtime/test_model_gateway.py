from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from app.model_runtime import ModelRuntimeSnapshot, compile_snapshot
from app.services.model_gateway import (
    AgentModelEvent,
    AgentModelGateway,
    AgentModelRequest,
)
from yoli_llm import SSEFrame
from yoli_llm.errors import ProviderError


def _snapshot(protocol: str = "openai_chat") -> ModelRuntimeSnapshot:
    return compile_snapshot(
        {
            "schema_version": 1,
            "revision": 7,
            "activated_at": datetime(2026, 7, 15, tzinfo=UTC),
            "routes": {
                "agent_main": {
                    "route_id": "main",
                    "protocol": protocol,
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
                        "max_retries": 1,
                    },
                    "tokenizer": {
                        "kind": "conservative_estimate",
                        "identifier": "gateway-test-tokenizer",
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
        },
        "agent_main",
    )


def _request(**overrides: Any) -> AgentModelRequest:
    values: dict[str, Any] = {
        "request_id": "req-redacted",
        "task_id": "task-redacted",
        "operation_key": "op-redacted",
        "purpose": "agent_main",
        "config_revision": 7,
        "route_id": "main",
        "model": "model-redacted",
        "system": "redacted system",
        "messages": [{"role": "user", "content": "redacted question"}],
        "max_output_tokens": 256,
    }
    values.update(overrides)
    return AgentModelRequest(**values)


async def _frames(frames: list[SSEFrame]) -> AsyncIterator[SSEFrame]:
    for frame in frames:
        yield frame


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_gateway_stream_projects_text_usage_and_stop() -> None:
    seen: dict[str, Any] = {}

    async def transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
        seen["protocol"] = request.protocol
        seen["body"] = dict(request.body)
        seen["credential"] = resolver(request.credential_handle)
        yield SSEFrame(
            data={
                "choices": [
                    {"delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}
                ]
            }
        )
        yield SSEFrame(data={"choices": [{"delta": {}, "finish_reason": "stop"}]})
        yield SSEFrame(data={"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        yield SSEFrame(done=True)

    gateway = AgentModelGateway(
        _snapshot(),
        endpoint="https://provider.example/v1",
        credential_resolver=lambda handle: "secret-not-in-events",
        transport=transport,
    )
    events = [event async for event in gateway.stream(_request())]
    assert [event.type for event in events] == [
        "message_start",
        "text_delta",
        "usage",
        "message_stop",
    ]
    assert events[1].payload == {"text": "hello"}
    assert events[2].payload["inputTokens"] == 3
    assert events[-1].payload["stopReason"] == "end_turn"
    assert seen["protocol"] == "openai_chat"
    assert seen["body"]["stream"] is True
    assert seen["credential"] == "secret-not-in-events"
    assert all("secret-not-in-events" not in str(event.as_dict()) for event in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_gateway_maps_tool_arguments_usage_and_stop() -> None:
    async def transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
        yield SSEFrame(data={"type": "message_start", "message": {"usage": {"input_tokens": 4}}})
        yield SSEFrame(
            data={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tool-redacted", "name": "lookup"},
            }
        )
        yield SSEFrame(
            data={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"paris"}'},
            }
        )
        yield SSEFrame(data={"type": "content_block_stop", "index": 0})
        yield SSEFrame(
            data={
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 3},
            }
        )
        yield SSEFrame(data={"type": "message_stop"})

    gateway = AgentModelGateway(
        _snapshot("anthropic_messages"),
        endpoint="https://provider.example/v1",
        credential_resolver=lambda handle: "secret",
        transport=transport,
    )
    events = [event async for event in gateway.stream(_request())]
    tool_stop = next(event for event in events if event.type == "tool_stop")
    assert tool_stop.payload["toolUseId"] == "tool-redacted"
    assert tool_stop.payload["input"] == {"q": "paris"}
    assert next(event for event in events if event.type == "usage").payload == {
        "inputTokens": 4,
        "outputTokens": 3,
        "cacheReadTokens": 0,
        "estimated": False,
    }
    assert events[-1].payload["stopReason"] == "tool_use"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_rejects_identity_before_transport_and_maps_error() -> None:
    called = False

    async def transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
        nonlocal called
        called = True
        yield SSEFrame(done=True)

    gateway = AgentModelGateway(
        _snapshot(),
        endpoint="https://provider.example/v1",
        credential_resolver=lambda handle: "secret",
        transport=transport,
    )
    events = [event async for event in gateway.stream(_request(config_revision=8))]
    assert called is False
    assert events == [
        AgentModelEvent(
            type="error",
            request_id="req-redacted",
            payload={
                "code": "MODEL_REQUEST_IDENTITY_MISMATCH",
                "retryable": False,
                "message": "request revision or route does not match snapshot",
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_maps_post_start_provider_error_without_retry() -> None:
    calls = 0

    async def transport(request: Any, resolver: Any) -> AsyncIterator[SSEFrame]:
        nonlocal calls
        calls += 1
        yield SSEFrame(data={"choices": [{"delta": {"content": "first"}, "finish_reason": None}]})
        raise ProviderError("provider said Bearer sk-secret", provider="test")

    gateway = AgentModelGateway(
        _snapshot(),
        endpoint="https://provider.example/v1",
        credential_resolver=lambda handle: "secret",
        transport=transport,
    )
    events = [event async for event in gateway.stream(_request())]
    assert calls == 1
    assert [event.type for event in events] == ["message_start", "text_delta", "error"]
    assert events[-1].payload == {
        "code": "MODEL_UPSTREAM_ERROR",
        "retryable": True,
        "message": "model provider returned an error",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_token_count_is_explicitly_estimated() -> None:
    gateway = AgentModelGateway(
        _snapshot(),
        endpoint="https://provider.example/v1",
        credential_resolver=lambda handle: "secret",
        transport=lambda request, resolver: _frames([]),
    )
    result = await gateway.count_tokens(_request())
    assert result.estimated is True
    assert result.tokenizer == "gateway-test-tokenizer"
    assert result.tokens >= 16
