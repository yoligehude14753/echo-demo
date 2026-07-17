"""B02-E1：配置 convergence、snapshot immutability 和 identity pinning。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.model_runtime.config import (
    canonical_config_hash,
    compile_snapshot,
    normalize_model_runtime_config,
)
from app.model_runtime.errors import (
    MODEL_AUTH_MISSING,
    MODEL_CONFIG_STALE_REVISION,
    MODEL_ENDPOINT_CONFLICT,
    MODEL_ENDPOINT_MISSING,
    MODEL_REQUEST_IDENTITY_MISMATCH,
    ModelRuntimeConfigError,
    ModelRuntimeRequestIdentityError,
    ModelRuntimeStaleRevisionError,
)
from app.model_runtime.snapshot import validate_request_identity
from pydantic import ValidationError


def _capabilities() -> dict[str, bool]:
    return {
        "streaming": True,
        "tool_use": True,
        "parallel_tool_use": True,
        "tool_choice": True,
        "system_messages": True,
        "usage_in_stream": True,
        "prompt_cache": False,
        "multimodal_images": False,
        "multimodal_documents": False,
    }


def _route(
    *,
    route_id: str = "main",
    credential_handle: str = "credential://primary",
    endpoint: str = "https://provider.example/v1/",
    model: str = "model-main",
) -> dict[str, object]:
    return {
        "route_id": route_id,
        "protocol": "openai_chat",
        "base_url": endpoint,
        "credential_handle": credential_handle,
        "model": model,
        "fallback_route_ids": [],
        "capabilities": _capabilities(),
        "limits": {
            "context_window": 128_000,
            "max_output_tokens": 8_000,
            "request_timeout_s": 120.0,
            "max_retries": 2,
        },
        "tokenizer": {
            "kind": "conservative_estimate",
            "identifier": "unit-tokenizer",
            "estimated": True,
            "safety_margin_tokens": 512,
        },
        "reasoning": {
            "mode": "none",
            "strip_think_tags": True,
            "token_budget": None,
        },
    }


def _config(*, credential_handle: str = "credential://primary", revision: int = 7) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": revision,
        "routes": {"agent_main": _route(credential_handle=credential_handle)},
        "activated_at": datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
    }


@pytest.mark.unit
def test_b02_e1_same_normalized_input_has_stable_hash_and_snapshot() -> None:
    first = compile_snapshot(_config(), "agent_main")
    second = compile_snapshot(_config(), "agent_main")

    assert first == second
    assert first.config_hash == canonical_config_hash(normalize_model_runtime_config(_config()))
    assert first.revision == 7
    assert first.route_id == "main"
    assert first.protocol == "openai_chat"
    assert first.public_dict()["routeId"] == "main"
    assert "credentialHandle" not in first.public_dict()


@pytest.mark.unit
def test_credential_handle_is_opaque_and_never_hash_or_error_material() -> None:
    secret_like = "sk-unit-secret-must-not-escape"
    with pytest.raises(ModelRuntimeConfigError) as error:
        compile_snapshot(_config(credential_handle=secret_like), "agent_main")
    assert error.value.code != "MODEL_CONFIG_INVALID"
    assert secret_like not in str(error.value)

    left = compile_snapshot(_config(credential_handle="credential://left"), "agent_main")
    right = compile_snapshot(_config(credential_handle="credential://right"), "agent_main")
    assert left.config_hash == right.config_hash
    assert "credential://left" not in repr(left)
    assert "credential://right" not in repr(right)


@pytest.mark.unit
def test_snapshot_and_nested_contract_values_are_immutable() -> None:
    snapshot = compile_snapshot(_config(), "agent_main")

    with pytest.raises(ValidationError):
        snapshot.revision = 8  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.capabilities.streaming = False  # type: ignore[misc]
    assert isinstance(normalize_model_runtime_config(_config()).routes["agent_main"].fallback_route_ids, tuple)


@pytest.mark.unit
def test_missing_or_conflicting_endpoint_and_auth_fail_closed() -> None:
    base_url_alias = _config()
    route = base_url_alias["routes"]["agent_main"]  # type: ignore[index]
    route["baseUrl"] = route.pop("base_url")  # type: ignore[index]
    assert compile_snapshot(base_url_alias, "agent_main").route_id == "main"

    equivalent_aliases = _config()
    route = equivalent_aliases["routes"]["agent_main"]  # type: ignore[index]
    route["baseUrl"] = route["base_url"]  # type: ignore[index]
    assert compile_snapshot(equivalent_aliases, "agent_main").route_id == "main"

    missing_endpoint = _config()
    route = missing_endpoint["routes"]["agent_main"]  # type: ignore[index]
    del route["base_url"]  # type: ignore[index]
    with pytest.raises(ModelRuntimeConfigError) as endpoint_error:
        compile_snapshot(missing_endpoint, "agent_main")
    assert endpoint_error.value.code == MODEL_ENDPOINT_MISSING

    conflicting_endpoint = _config()
    route = conflicting_endpoint["routes"]["agent_main"]  # type: ignore[index]
    route["endpoint"] = "https://other.example/v1"  # type: ignore[index]
    with pytest.raises(ModelRuntimeConfigError) as conflict_error:
        compile_snapshot(conflicting_endpoint, "agent_main")
    assert conflict_error.value.code == MODEL_ENDPOINT_CONFLICT

    conflicting_base_alias = _config()
    route = conflicting_base_alias["routes"]["agent_main"]  # type: ignore[index]
    route["baseUrl"] = "https://other.example/v1"  # type: ignore[index]
    with pytest.raises(ModelRuntimeConfigError) as base_alias_error:
        compile_snapshot(conflicting_base_alias, "agent_main")
    assert base_alias_error.value.code == MODEL_ENDPOINT_CONFLICT

    missing_auth = _config()
    route = missing_auth["routes"]["agent_main"]  # type: ignore[index]
    del route["credential_handle"]  # type: ignore[index]
    with pytest.raises(ModelRuntimeConfigError) as auth_error:
        compile_snapshot(missing_auth, "agent_main")
    assert auth_error.value.code == MODEL_AUTH_MISSING


@pytest.mark.unit
def test_stale_revision_and_request_identity_mismatch_fail_closed() -> None:
    snapshot = compile_snapshot(_config(revision=9), "agent_main", expected_revision=9)
    with pytest.raises(ModelRuntimeStaleRevisionError) as stale_error:
        compile_snapshot(_config(revision=9), "agent_main", expected_revision=8)
    assert stale_error.value.code == MODEL_CONFIG_STALE_REVISION

    identity = snapshot.identity(
        request_id="request-1",
        task_id="task-1",
        operation_key="operation-1",
    )
    assert identity.model_dump(by_alias=True) == {
        "requestId": "request-1",
        "taskId": "task-1",
        "operationKey": "operation-1",
        "configRevision": 9,
        "routeId": "main",
    }
    assert validate_request_identity(identity, snapshot) == identity

    stale_identity = identity.model_copy(update={"config_revision": 8})
    with pytest.raises(ModelRuntimeRequestIdentityError) as identity_error:
        validate_request_identity(stale_identity, snapshot)
    assert identity_error.value.code == MODEL_REQUEST_IDENTITY_MISMATCH


@pytest.mark.unit
def test_compact_and_summary_without_explicit_routes_alias_main() -> None:
    snapshot = compile_snapshot(_config(), "agent_compact")
    summary = compile_snapshot(_config(), "agent_summary")

    assert snapshot.route_id == "main"
    assert summary.route_id == "main"
    assert snapshot.revision == summary.revision == 7
