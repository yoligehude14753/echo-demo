"""B02-E2/E3 protocol adapter contract tests; no network or tool host."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from app.model_runtime.protocols import (
    ModelEventEnvelope,
    ModelRequestEnvelope,
    ProtocolAdapterError,
    RequestIdentity,
    build_anthropic_request,
    build_openai_compatible_request,
    normalize_anthropic_stream,
    normalize_openai_compatible_stream,
)
from app.model_runtime.types import RequestIdentity as CanonicalRequestIdentity

FIXTURES = Path(__file__).parent / "fixtures"


def _identity() -> CanonicalRequestIdentity:
    return CanonicalRequestIdentity(
        requestId="req-redacted",
        taskId="task-redacted",
        operationKey="op-redacted",
        configRevision=7,
        routeId="route-test",
    )


def _jsonl(name: str) -> list[str]:
    return [
        line for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _error_code(events: tuple[Any, ...]) -> str:
    assert events[-1].type == "error"
    return str(events[-1].payload["code"])


def _semantic_summary(events: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "text": "".join(event.payload["text"] for event in events if event.type == "text_delta"),
        "tools": [
            {
                "id": event.payload["toolUseId"],
                "name": event.payload["tool"]["name"],
                "input": event.payload["input"],
            }
            for event in events
            if event.type == "tool_stop"
        ],
        "usage": [event.payload for event in events if event.type == "usage"],
        "stop": [event.payload["stopReason"] for event in events if event.type == "message_stop"],
    }


@pytest.mark.unit
def test_request_conversion_preserves_identity_and_provider_shapes() -> None:
    identity = _identity()
    tools = [
        {
            "name": "lookup",
            "description": "redacted lookup tool",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    ]
    messages = [{"role": "user", "content": "redacted question"}]

    anthropic = build_anthropic_request(
        identity,
        model="claude-test",
        system="redacted system",
        messages=messages,
        tools=tools,
        max_output_tokens=256,
    )
    openai = build_openai_compatible_request(
        identity,
        model="openai-compatible-test",
        system="redacted system",
        messages=messages,
        tools=tools,
        max_output_tokens=256,
    )

    assert RequestIdentity is CanonicalRequestIdentity
    assert anthropic.identity is identity
    assert openai.identity is identity
    for request in (anthropic, openai):
        assert request.envelope.schema_version == 1
        assert request.envelope.request_id == "req-redacted"
        assert request.envelope.task_id == "task-redacted"
        assert request.envelope.operation_key == "op-redacted"
        assert request.envelope.config_revision == 7
        assert request.envelope.route_id == "route-test"
        assert request.body["stream"] is True
        assert "redacted" in json.dumps(request.envelope.as_dict()["body"], ensure_ascii=False)
    assert anthropic.body["tools"][0]["input_schema"]["type"] == "object"
    assert openai.body["tools"][0]["function"]["parameters"]["type"] == "object"
    assert openai.body["stream_options"] == {"include_usage": True}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fixture", "normalizer"),
    [
        ("anthropic_stream.jsonl", normalize_anthropic_stream),
        ("openai_stream.jsonl", normalize_openai_compatible_stream),
    ],
)
def test_stream_replay_has_common_text_tool_usage_stop_contract(
    fixture: str,
    normalizer: Callable[..., tuple[Any, ...]],
) -> None:
    identity = _identity()
    events = normalizer(_jsonl(fixture), identity)
    assert events[-1].type == "message_stop"
    assert all(event.schema_version == 1 for event in events)
    assert all(event.identity is identity for event in events)

    summary = _semantic_summary(events)
    expected_text = (
        "Hello from model." if fixture.startswith("anthropic") else "Hello from compatible model."
    )
    expected_tool_ids = (
        ["toolu_redacted_1", "toolu_redacted_2"]
        if fixture.startswith("anthropic")
        else ["call_redacted_1", "call_redacted_2"]
    )
    assert summary["text"] == expected_text
    assert [tool["id"] for tool in summary["tools"]] == expected_tool_ids
    assert summary["tools"][0]["input"] == {"q": "paris"}
    assert summary["tools"][1]["input"] == {"q": "tokyo"}
    assert summary["usage"][0]["inputTokens"] == 12
    assert summary["usage"][0]["outputTokens"] == 9
    assert summary["usage"][0]["cacheReadTokens"] == 3
    assert summary["stop"] == ["tool_use"]
    assert all(event.identity.request_id == "req-redacted" for event in events)
    assert all("credential" not in json.dumps(event.as_dict()).lower() for event in events)


@pytest.mark.unit
def test_envelopes_reject_missing_fields_and_identity_mismatch() -> None:
    identity = _identity()
    with pytest.raises(ProtocolAdapterError, match="MODEL_SCHEMA_VERSION_MISMATCH"):
        ModelRequestEnvelope(identity=identity)
    with pytest.raises(ProtocolAdapterError, match="MODEL_SCHEMA_VERSION_MISMATCH"):
        ModelEventEnvelope(identity=identity)
    with pytest.raises(ProtocolAdapterError, match="MODEL_REQUEST_IDENTITY_MISMATCH"):
        ModelRequestEnvelope(
            identity=identity,
            schema_version=1,
            task_id="task-other",
            operation_key=identity.operation_key,
            request_id=identity.request_id,
            config_revision=identity.config_revision,
            route_id=identity.route_id,
            body={},
        )
    with pytest.raises(ProtocolAdapterError, match="MODEL_REQUEST_IDENTITY_MISMATCH"):
        ModelEventEnvelope(
            identity=identity,
            schema_version=1,
            task_id=identity.task_id,
            operation_key=identity.operation_key,
            request_id="req-other",
            config_revision=identity.config_revision,
            route_id=identity.route_id,
            type="text_delta",
            payload={"text": "redacted"},
        )


@pytest.mark.unit
def test_unknown_chunk_schema_and_request_identity_fail_closed() -> None:
    identity = _identity()
    start = {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}
    unknown = normalize_anthropic_stream([start, {"type": "future_provider_chunk"}], identity)
    assert _error_code(unknown) == "MODEL_SCHEMA_VERSION_MISMATCH"

    schema_mismatch = normalize_openai_compatible_stream(
        [{"schemaVersion": 2, "choices": []}], identity
    )
    assert _error_code(schema_mismatch) == "MODEL_SCHEMA_VERSION_MISMATCH"

    request_mismatch = normalize_openai_compatible_stream(
        [
            {
                "requestId": "req-other",
                "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}],
            }
        ],
        identity,
    )
    assert _error_code(request_mismatch) == "MODEL_REQUEST_IDENTITY_MISMATCH"


@pytest.mark.unit
def test_tool_arguments_are_typed_only_and_never_executed() -> None:
    invocations: list[dict[str, Any]] = []
    events = normalize_anthropic_stream(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu-redacted",
                    "name": "lookup",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
            },
            {"type": "content_block_stop", "index": 0},
        ],
        _identity(),
    )
    assert _error_code(events) == "MODEL_TOOL_ARGUMENTS_INVALID"
    assert not any(event.type == "tool_stop" for event in events)
    assert invocations == []

    identity = _identity()
    valid = normalize_anthropic_stream(_jsonl("anthropic_stream.jsonl"), identity)
    tool_stop = next(event for event in valid if event.type == "tool_stop")
    assert tool_stop.payload["tool"]["toolUseId"] == "toolu_redacted_1"
    assert tool_stop.identity is identity


@pytest.mark.unit
def test_provider_errors_are_normalized_and_redacted() -> None:
    anthropic = normalize_anthropic_stream(_jsonl("anthropic_error.jsonl"), _identity())
    openai = normalize_openai_compatible_stream(_jsonl("openai_error.jsonl"), _identity())
    for events, expected_retryable in ((anthropic, False), (openai, True)):
        assert _error_code(events) == "MODEL_UPSTREAM_ERROR"
        assert events[-1].payload["retryable"] is expected_retryable
        message = str(events[-1].payload["message"])
        assert "sk-redacted" not in message
        assert "Bearer redacted-token" not in message


@pytest.mark.unit
def test_openai_missing_usage_is_explicitly_not_guessed() -> None:
    events = normalize_openai_compatible_stream(
        [
            {"choices": [{"delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "data: [DONE]",
        ],
        _identity(),
    )
    assert [event.type for event in events] == ["message_start", "text_delta", "message_stop"]
    assert not any(event.type == "usage" for event in events)
    assert events[-1].payload["stopReason"] == "end_turn"


@pytest.mark.unit
def test_http_error_mapping_uses_stable_codes_without_provider_messages() -> None:
    fixture = json.loads((FIXTURES / "error_mapping.json").read_text(encoding="utf-8"))
    for case in fixture["cases"]:
        wire_error = dict(case["wire_error"])
        wire_error["status"] = case["http_status"]
        normalize = (
            normalize_anthropic_stream
            if case["protocol"] == "anthropic_messages"
            else normalize_openai_compatible_stream
        )
        events = normalize([wire_error], _identity())
        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].payload["code"] == case["expected"]["code"]
        assert events[0].payload["retryable"] is case["expected"]["retryable"]
        assert (
            events[0].payload["message"]
            == {
                "MODEL_CREDENTIAL_MISSING": "model credential is missing or rejected",
                "MODEL_CREDENTIAL_REVOKED": "model credential was revoked",
                "MODEL_TIMEOUT": "model provider timed out",
                "MODEL_UPSTREAM_ERROR": "model provider returned an error",
            }[case["expected"]["code"]]
        )
