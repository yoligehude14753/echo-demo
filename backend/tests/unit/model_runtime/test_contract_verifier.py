"""B02 model-runtime contract verifier.

这些测试只消费冻结合同对应的脱敏 wire-shape fixture，不实现或替代 production
adapter。fixture 中的 ``expected.tool_invocations == 0`` 是协议层不得执行 tool
的显式证据；待 B02 前两项 public API 落地后，应将同一 fixture 接入真实 API。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from app.model_runtime import (
    ModelRuntimeSnapshot,
    RequestIdentity,
    assert_snapshot_revision,
    compile_snapshot,
)
from app.model_runtime.errors import ModelRuntimeStaleRevisionError
from app.model_runtime.protocols import (
    ModelEventEnvelope,
    normalize_anthropic_stream,
    normalize_openai_compatible_stream,
)
from pydantic import ValidationError

FIXTURE_DIR = Path(__file__).with_name("fixtures")
EXPECTED_SNAPSHOT_FIELDS = {
    "schemaVersion",
    "revision",
    "configHash",
    "purpose",
    "routeId",
    "protocol",
    "model",
    "capabilities",
    "limits",
    "tokenizer",
    "reasoning",
    "credentialHandle",
}
EXPECTED_REQUEST_IDENTITY_FIELDS = {
    "requestId",
    "taskId",
    "operationKey",
    "configRevision",
    "routeId",
}
SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|bearer\s+[A-Za-z0-9._-]{20,}|-----BEGIN [A-Z ]+ KEY-----)",
    re.IGNORECASE,
)
FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "raw_credential",
    "secret_value",
    "access_token",
}


def _load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURE_DIR / name).open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _load_manifest_fixture(name: str) -> Any:
    if not name.endswith(".jsonl"):
        return _load_fixture(name)
    records: list[Any] = []
    for raw_line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        normalized_line = raw_line.strip()
        if not normalized_line:
            continue
        if normalized_line.startswith("data:"):
            normalized_line = normalized_line.removeprefix("data:").strip()
        if normalized_line == "[DONE]":
            records.append(normalized_line)
            continue
        records.append(json.loads(normalized_line))
    return records


def _fixture_sha256(name: str) -> str:
    return hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()


def _assert_redacted(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in FORBIDDEN_SECRET_KEYS, f"secret-bearing key at {path}.{key}"
            _assert_redacted(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_redacted(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        assert not SECRET_VALUE_PATTERN.search(value), f"secret-like value at {path}"


def _request_identity(fixture: dict[str, Any]) -> RequestIdentity:
    return RequestIdentity.model_validate(fixture["request_identity"])


def _runtime_config(fixture: dict[str, Any]) -> dict[str, Any]:
    snapshot = fixture["snapshot"]
    capabilities = snapshot["capabilities"]
    limits = snapshot["limits"]
    tokenizer = snapshot["tokenizer"]
    reasoning = snapshot["reasoning"]
    return {
        "schemaVersion": 1,
        "revision": snapshot["revision"],
        "activatedAt": "2026-07-15T00:00:00Z",
        "routes": {
            snapshot["purpose"]: {
                "routeId": snapshot["routeId"],
                "protocol": snapshot["protocol"],
                "baseUrl": "https://api.example.invalid/v1",
                "credentialHandle": snapshot["credentialHandle"],
                "model": snapshot["model"],
                "fallbackRouteIds": [],
                "capabilities": {
                    "streaming": capabilities["streaming"],
                    "toolUse": capabilities["tool_use"],
                    "parallelToolUse": capabilities["parallel_tool_use"],
                    "toolChoice": capabilities["tool_choice"],
                    "systemMessages": capabilities["system_messages"],
                    "usageInStream": capabilities["usage_in_stream"],
                    "promptCache": capabilities["prompt_cache"],
                    "multimodalImages": capabilities["multimodal_images"],
                    "multimodalDocuments": capabilities["multimodal_documents"],
                },
                "limits": {
                    "contextWindow": limits["context_window"],
                    "maxOutputTokens": limits["max_output_tokens"],
                    "requestTimeoutS": limits["request_timeout_s"],
                    "maxRetries": limits["max_retries"],
                },
                "tokenizer": {
                    "kind": tokenizer["kind"],
                    "identifier": tokenizer["identifier"],
                    "estimated": tokenizer["estimated"],
                    "safetyMarginTokens": tokenizer["safety_margin_tokens"],
                },
                "reasoning": {
                    "mode": reasoning["mode"],
                    "stripThinkTags": reasoning["strip_think_tags"],
                    "tokenBudget": reasoning["token_budget"],
                },
            }
        },
    }


def _snapshot(fixture: dict[str, Any]) -> ModelRuntimeSnapshot:
    return compile_snapshot(_runtime_config(fixture), fixture["snapshot"]["purpose"])


def _event_dicts(events: tuple[ModelEventEnvelope, ...]) -> list[dict[str, Any]]:
    identity_fields = {
        "schemaVersion",
        "taskId",
        "operationKey",
        "requestId",
        "configRevision",
        "routeId",
        "type",
    }
    normalized: list[dict[str, Any]] = []
    for item in events:
        raw = item.as_dict()
        normalized.append(
            {
                **{field: raw[field] for field in identity_fields if field in raw},
                "payload": {
                    field: value for field, value in raw.items() if field not in identity_fields
                },
            }
        )
    return normalized


@pytest.mark.unit
def test_fixture_manifest_hashes_and_redaction_are_stable() -> None:
    manifest = _load_fixture("fixture_manifest.json")
    assert manifest["schema"] == "b02-model-runtime-fixture-manifest.v1"

    for entry in manifest["fixtures"]:
        fixture_name = entry["path"]
        assert _fixture_sha256(fixture_name) == entry["sha256"]
        _assert_redacted(_load_manifest_fixture(fixture_name), fixture_name)


@pytest.mark.unit
def test_anthropic_golden_replays_parallel_tool_use_usage_and_finish_reason() -> None:
    fixture = _load_fixture("anthropic_messages_stream.json")
    stream = fixture["stream"]
    expected = fixture["expected"]

    text_parts: list[str] = []
    tool_starts: dict[int, dict[str, Any]] = {}
    tool_fragments: defaultdict[int, list[str]] = defaultdict(list)
    input_tokens = 0
    output_tokens = 0
    finish_reason: str | None = None

    for item in stream:
        event = item["event"]
        data = item["data"]
        if event == "message_start":
            input_tokens = data["message"]["usage"]["input_tokens"]
        elif event == "content_block_start":
            block = data["content_block"]
            if block["type"] == "tool_use":
                tool_starts[data["index"]] = {
                    "index": data["index"],
                    "tool_use_id": block["id"],
                    "name": block["name"],
                }
        elif event == "content_block_delta":
            delta = data["delta"]
            if delta["type"] == "text_delta":
                text_parts.append(delta["text"])
            elif delta["type"] == "input_json_delta":
                tool_fragments[data["index"]].append(delta["partial_json"])
        elif event == "message_delta":
            finish_reason = data["delta"]["stop_reason"]
            output_tokens = data["usage"]["output_tokens"]

    tool_calls = []
    for index in sorted(tool_starts):
        call = dict(tool_starts[index])
        call["input"] = json.loads("".join(tool_fragments[index]))
        tool_calls.append(call)

    assert tool_calls == expected["tool_calls"]
    assert "".join(text_parts) == expected["text"]
    assert {
        **expected["usage"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    } == expected["usage"]
    assert finish_reason == expected["finish_reason"]
    assert expected["tool_invocations"] == 0

    events = normalize_anthropic_stream(
        [item["data"] for item in stream], _request_identity(fixture)
    )
    event_dicts = _event_dicts(events)
    assert all(
        event["requestId"] == fixture["request_identity"]["request_id"] for event in event_dicts
    )
    assert [event["type"] for event in event_dicts][-1] == "message_stop"
    assert [event["payload"]["text"] for event in event_dicts if event["type"] == "text_delta"] == [
        expected["text"]
    ]
    actual_tool_stops = [event["payload"] for event in event_dicts if event["type"] == "tool_stop"]
    assert [payload["input"] for payload in actual_tool_stops] == [
        call["input"] for call in expected["tool_calls"]
    ]
    usage_events = [event["payload"] for event in event_dicts if event["type"] == "usage"]
    assert usage_events == [
        {"inputTokens": 21, "outputTokens": 14, "cacheReadTokens": 0, "estimated": False}
    ]
    assert not [event for event in event_dicts if event["type"] == "tool_invoke"]


@pytest.mark.unit
def test_openai_golden_replays_parallel_tool_calls_usage_and_finish_reason() -> None:
    fixture = _load_fixture("openai_chat_stream.json")
    expected = fixture["expected"]
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    arguments: defaultdict[int, list[str]] = defaultdict(list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None

    for chunk in fixture["stream"]:
        usage = chunk.get("usage", usage)
        for choice in chunk["choices"]:
            delta = choice["delta"]
            if delta.get("content"):
                text_parts.append(delta["content"])
            for tool_call in delta.get("tool_calls", []):
                index = tool_call["index"]
                current = tool_calls.setdefault(index, {"index": index})
                if "id" in tool_call:
                    current["tool_use_id"] = tool_call["id"]
                function = tool_call.get("function", {})
                if "name" in function:
                    current["name"] = function["name"]
                if "arguments" in function:
                    arguments[index].append(function["arguments"])
            if choice["finish_reason"] is not None:
                finish_reason = choice["finish_reason"]

    normalized_calls = []
    for index in sorted(tool_calls):
        call = dict(tool_calls[index])
        call["input"] = json.loads("".join(arguments[index]))
        normalized_calls.append(call)

    assert normalized_calls == expected["tool_calls"]
    assert "".join(text_parts) == expected["text"]
    assert usage == {"prompt_tokens": 21, "completion_tokens": 14, "total_tokens": 35}
    assert expected["usage"] == {
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "cache_read_tokens": 0,
        "estimated": False,
    }
    assert finish_reason == expected["finish_reason"]
    assert expected["tool_invocations"] == 0

    events = normalize_openai_compatible_stream(fixture["stream"], _request_identity(fixture))
    event_dicts = _event_dicts(events)
    assert all(
        event["requestId"] == fixture["request_identity"]["request_id"] for event in event_dicts
    )
    assert [event["type"] for event in event_dicts][-1] == "message_stop"
    assert (
        "".join(event["payload"]["text"] for event in event_dicts if event["type"] == "text_delta")
        == expected["text"]
    )
    actual_tool_stops = [event["payload"] for event in event_dicts if event["type"] == "tool_stop"]
    assert [payload["input"] for payload in actual_tool_stops] == [
        call["input"] for call in expected["tool_calls"]
    ]
    usage_events = [event["payload"] for event in event_dicts if event["type"] == "usage"]
    assert usage_events == [
        {"inputTokens": 21, "outputTokens": 14, "cacheReadTokens": 0, "estimated": False}
    ]
    assert not [event for event in event_dicts if event["type"] == "tool_invoke"]


@pytest.mark.unit
def test_provider_errors_map_to_stable_codes_and_retryability() -> None:
    fixture = _load_fixture("error_mapping.json")
    expected_codes = {
        "MODEL_CREDENTIAL_MISSING",
        "MODEL_CREDENTIAL_REVOKED",
        "MODEL_UPSTREAM_ERROR",
        "MODEL_TIMEOUT",
    }
    for case in fixture["cases"]:
        expected = case["expected"]
        assert expected["code"] in expected_codes
        assert isinstance(expected["retryable"], bool)
        assert "message" not in expected
        _assert_redacted(case["wire_error"], case["name"])

        wire_error = deepcopy(case["wire_error"])
        wire_error["status"] = case["http_status"]
        identity = _request_identity(
            _load_fixture(
                "anthropic_messages_stream.json"
                if case["protocol"] == "anthropic_messages"
                else "openai_chat_stream.json"
            )
        )
        normalize = (
            normalize_anthropic_stream
            if case["protocol"] == "anthropic_messages"
            else normalize_openai_compatible_stream
        )
        events = _event_dicts(normalize([wire_error], identity))
        error_events = [event for event in events if event["type"] == "error"]
        assert len(error_events) == 1
        assert error_events[0]["payload"]["code"] == expected["code"]
        assert error_events[0]["payload"]["retryable"] == expected["retryable"]


@pytest.mark.unit
def test_snapshot_contract_is_complete_immutable_by_revision_and_secret_free() -> None:
    fixture = _load_fixture("snapshot_and_negative_cases.json")
    snapshot = fixture["snapshot"]
    assert set(snapshot) == EXPECTED_SNAPSHOT_FIELDS
    assert snapshot["schemaVersion"] == 1
    assert isinstance(snapshot["revision"], int) and snapshot["revision"] >= 1
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot["configHash"])
    assert snapshot["credentialHandle"].startswith("cred://")
    assert snapshot["credentialHandle"] != snapshot["configHash"]
    _assert_redacted(snapshot)

    compiled = _snapshot(fixture)
    assert compiled.revision == snapshot["revision"]
    assert compiled.route_id == snapshot["routeId"]
    assert "credentialHandle" not in compiled.public_dict()
    with pytest.raises(ValidationError):
        compiled.revision = snapshot["revision"] + 1
    with pytest.raises(ModelRuntimeStaleRevisionError) as stale:
        assert_snapshot_revision(compiled, snapshot["revision"] - 1)
    assert stale.value.code == "MODEL_CONFIG_STALE_REVISION"


@pytest.mark.unit
def test_one_canonical_request_identity_rejects_duplicate_variant() -> None:
    fixture = _load_fixture("snapshot_and_negative_cases.json")
    canonical = fixture["canonical_request_identity"]
    duplicate = fixture["duplicate_request_identity"]

    assert set(canonical) == EXPECTED_REQUEST_IDENTITY_FIELDS
    assert duplicate["canonical"] == canonical
    assert duplicate["conflicting_variant"]["requestId"] == canonical["requestId"]
    assert duplicate["conflicting_variant"] != canonical
    assert duplicate["expected"] == {
        "action": "reject",
        "code": "MODEL_REQUEST_ID_MISMATCH",
        "tool_invocations": 0,
    }


@pytest.mark.unit
def test_model_negative_cases_fail_closed_without_protocol_tool_execution() -> None:
    fixture = _load_fixture("snapshot_and_negative_cases.json")
    canonical = fixture["canonical_request_identity"]

    for case in fixture["negative_cases"]:
        expected = case["expected"]
        assert expected["action"] == "reject", case["name"]
        assert expected["tool_invocations"] == 0, case["name"]

        if case["name"] == "stale_revision":
            assert case["input"]["configRevision"] < case["input"]["activeConfigRevision"]
        if case["name"] == "request_id_mismatch":
            assert case["input"]["requestId"] != case["input"]["activeRequestId"]
        if case["name"].startswith("tool_result_"):
            assert expected["code"] == "MODEL_TOOL_CORRELATION_MISMATCH"

    for case in fixture["request_envelope_negative_cases"]:
        envelope = case["envelope"]
        expected = case["expected"]
        assert expected["action"] == "reject", case["name"]
        assert expected["tool_invocations"] == 0, case["name"]
        if "missing" in expected:
            assert expected["missing"] not in envelope
        elif expected.get("mismatch"):
            field = expected["mismatch"]
            assert envelope[field] != canonical[field], case["name"]
        else:
            assert envelope["schemaVersion"] != 1, case["name"]

    assert fixture["duplicate_request_identity"]["expected"]["tool_invocations"] == 0
