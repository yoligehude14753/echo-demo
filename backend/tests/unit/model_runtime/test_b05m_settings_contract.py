"""B05M C-owned Settings/identity/contract verification.

This suite consumes the B02 pure compiler and envelope types. It intentionally
does not replay provider golden streams or invoke any transport.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from app.model_runtime import (
    canonical_config_hash,
    compile_model_runtime_config,
    compile_snapshot,
)
from app.model_runtime.protocols import (
    ModelEventEnvelope,
    ModelRequestEnvelope,
    ProtocolAdapterError,
)
from app.model_runtime.types import RequestIdentity

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURE_DIR / "b05m_settings_contract.json"
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)api[_ -]?key\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)token\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)password\s*[:=]\s*[^\s,;]+"),
)


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def assert_secret_free(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in {"credentialhandle", "apikey", "token", "secret"}
            assert_secret_free(child)
    elif isinstance(value, list):
        for child in value:
            assert_secret_free(child)
    elif isinstance(value, str):
        assert not any(pattern.search(value) for pattern in SECRET_PATTERNS)


def canonical_identity(fixture: dict[str, Any]) -> RequestIdentity:
    projection = fixture["identity_projection"]
    return RequestIdentity(
        requestId="req-settings-contract",
        taskId="task-settings-contract",
        operationKey="op-settings-contract",
        configRevision=projection["revision"],
        routeId=projection["routeId"],
    )


def request_envelope(raw: dict[str, Any], identity: RequestIdentity) -> ModelRequestEnvelope:
    return ModelRequestEnvelope(
        identity=identity,
        schema_version=raw.get("schemaVersion"),
        task_id=raw.get("taskId", ""),
        operation_key=raw.get("operationKey", ""),
        request_id=raw.get("requestId", ""),
        config_revision=raw.get("configRevision", 0),
        route_id=raw.get("routeId", ""),
        body=raw.get("body", {}),
    )


def event_envelope(raw: dict[str, Any], identity: RequestIdentity) -> ModelEventEnvelope:
    return ModelEventEnvelope(
        identity=identity,
        schema_version=raw.get("schemaVersion"),
        task_id=raw.get("taskId", ""),
        operation_key=raw.get("operationKey", ""),
        request_id=raw.get("requestId", ""),
        config_revision=raw.get("configRevision", 0),
        route_id=raw.get("routeId", ""),
        type=raw.get("type", ""),
        payload=raw.get("payload", {}),
    )


@pytest.mark.unit
def test_settings_config_round_trip_and_kernel_identity_projection() -> None:
    fixture = load_fixture()
    config = compile_model_runtime_config(fixture["config"])
    serialized = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    restored = compile_model_runtime_config(serialized)

    assert restored.revision == config.revision == fixture["identity_projection"]["revision"]
    assert restored.routes["agent_main"].route_id == fixture["identity_projection"]["routeId"]
    assert canonical_config_hash(restored) == canonical_config_hash(config)

    snapshot = compile_snapshot(restored, "agent_main")
    public = snapshot.public_dict()
    projection = fixture["identity_projection"]
    assert {key: public[key] for key in projection} == projection
    assert isinstance(public["configHash"], str) and len(public["configHash"]) == 64
    assert "credentialHandle" not in public
    assert_secret_free(projection)


@pytest.mark.unit
def test_public_model_payload_field_freeze_is_not_extended_by_b05m() -> None:
    fixture = load_fixture()
    assert fixture["public_agent_model_request_fields"] == [
        "requestId",
        "taskId",
        "purpose",
        "configRevision",
        "routeId",
        "model",
        "system",
        "messages",
        "tools",
        "toolChoice",
        "maxOutputTokens",
        "temperature",
        "stopSequences",
    ]
    assert fixture["public_agent_model_event_common_fields"] == ["type", "requestId"]
    assert "schemaVersion" not in fixture["public_agent_model_request_fields"]
    assert "operationKey" not in fixture["public_agent_model_request_fields"]


@pytest.mark.unit
def test_missing_unknown_and_mismatched_envelopes_fail_closed() -> None:
    fixture = load_fixture()
    identity = canonical_identity(fixture)
    base = {
        "schemaVersion": 1,
        "taskId": identity.task_id,
        "operationKey": identity.operation_key,
        "requestId": identity.request_id,
        "configRevision": identity.config_revision,
        "routeId": identity.route_id,
        "body": {},
    }
    event_base = {**base, "type": "text_delta", "payload": {"text": "safe"}}

    for case in fixture["envelope_negative_cases"]:
        raw = deepcopy(event_base if case["kind"] == "event" else base)
        raw.pop("body", None) if case["kind"] == "event" else None
        if "remove" in case:
            raw.pop(case["remove"], None)
        if "add" in case:
            raw.update(case["add"])
        if "replace" in case:
            raw.update(case["replace"])

        if "add" in case:
            constructor = ModelEventEnvelope if case["kind"] == "event" else ModelRequestEnvelope
            kwargs = {
                "identity": identity,
                "schema_version": 1,
                "task_id": identity.task_id,
                "operation_key": identity.operation_key,
                "request_id": identity.request_id,
                "config_revision": identity.config_revision,
                "route_id": identity.route_id,
                "type": "text_delta",
                "futureField": True,
            }
            with pytest.raises(TypeError):
                constructor(**kwargs)
            continue

        with pytest.raises(ProtocolAdapterError) as rejected:
            (event_envelope if case["kind"] == "event" else request_envelope)(raw, identity)
        assert rejected.value.code in {
            "MODEL_SCHEMA_VERSION_MISMATCH",
            "MODEL_REQUEST_IDENTITY_MISMATCH",
        }


@pytest.mark.unit
def test_fallback_event_is_explicit_and_secret_free() -> None:
    fixture = load_fixture()
    fallback = fixture["fallback_event"]
    assert fallback["type"] == "agent.model.fallback"
    assert fallback["configRevision"] == fixture["identity_projection"]["revision"]
    assert fallback["fromRouteId"] != fallback["toRouteId"]
    assert_secret_free(fallback)
