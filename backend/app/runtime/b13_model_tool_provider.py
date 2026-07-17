"""B13's explicit B05M/B06P provider binding and short smoke harness.

This module is intentionally an additive integration seam.  It does not
discover credentials, hosts, or a second model route.  The caller must supply
the authoritative model-runtime store and a credential-handle resolver; B06P
hosts are registered explicitly and remain the authority for tool policy and
receipts.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal

from yoli_llm import stream_sse

from app.agent_capabilities import (
    CapabilityHostRegistry,
    CapabilityInvocation,
    CapabilityName,
    CapabilityRequest,
    HostOutcome,
    InvocationBinding,
    OperationReceipt,
    make_receipt,
)
from app.agent_capabilities.hosts import FileReadHost, HostContext, ToolInvocation
from app.agent_capabilities.types import (
    CapabilityDecision,
    DecisionOutcome,
    DenyCode,
    PathRequest,
    PermissionRight,
)
from app.model_runtime import (
    CredentialHandle,
    CredentialResolver,
    ModelRuntimeConfigStore,
    TaskModelRevisionRegistry,
    validate_credential_handle,
)
from app.services.model_gateway import AgentModelGateway, AgentModelRequest

B13_YOLI_TRANSPORT_SHA: Final = "158844db23cc5884889233fb8bdd7d943f3002f9"
B13_MODEL_GATEWAY_SOURCE: Final = "B05M:app.services.model_gateway.AgentModelGateway"
B13_TOOL_HOST_SOURCE: Final = "B06P:app.agent_capabilities.CapabilityHostRegistry"
EXTERNAL_CREDENTIAL_PENDING: Final = "EXTERNAL_CREDENTIAL_PENDING"

SmokeStatus = Literal["PASS", "EXTERNAL_CREDENTIAL_PENDING", "FAIL"]


class B13ProviderBindingError(RuntimeError):
    """Stable, secret-free failure for an incomplete production binding."""

    def __init__(self, code: str, message: str = "B13 provider binding rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class B13ProviderBinding:
    """The concrete model/tool ports for one task-scoped runtime binding."""

    task_id: str
    model_gateway: AgentModelGateway
    tool_hosts: CapabilityHostRegistry
    snapshot: Any
    transport_sha: str = B13_YOLI_TRANSPORT_SHA
    model_gateway_source: str = B13_MODEL_GATEWAY_SOURCE
    tool_host_source: str = B13_TOOL_HOST_SOURCE


@dataclass(frozen=True, slots=True)
class B13SmokeResult:
    status: SmokeStatus
    code: str
    model_events: int = 0
    tool_receipt_result: str | None = None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


async def _b13_yoli_transport(
    request: Any, resolver: Callable[[str], str | Awaitable[str]]
) -> AsyncIterator[Any]:
    """Keep B05M's public yoli transport behind a JSON-safe body boundary."""

    normalized = replace(request, body=_jsonable(request.body))
    async for frame in stream_sse(normalized, resolver):
        yield frame


def _credential_callback(resolver: CredentialResolver) -> Callable[[str], str]:
    def resolve(raw_handle: str) -> str:
        handle: CredentialHandle = validate_credential_handle(raw_handle)
        # The secret is returned only to yoli_llm's transport callback.  It is
        # never placed in this binding, an event, an exception, or a receipt.
        return resolver.resolve(handle).value

    return resolve


def create_b13_provider_binding(
    *,
    task_id: str,
    config_store: ModelRuntimeConfigStore,
    credential_resolver: CredentialResolver,
    transport: Callable[..., AsyncIterator[Any]] | None = None,
) -> B13ProviderBinding:
    """Bind one B05M gateway and the explicit B06P host registry.

    ``transport`` exists only for deterministic focused source tests.  The
    production default remains the B05M gateway's ``yoli_llm.stream_sse``.
    """

    if not task_id.strip():
        raise B13ProviderBindingError("B13_TASK_ID_REQUIRED")
    if credential_resolver is None:
        raise B13ProviderBindingError(EXTERNAL_CREDENTIAL_PENDING)
    try:
        revisions = TaskModelRevisionRegistry(config_store)
        snapshot = revisions.begin_task(task_id, "agent_main")
        route = revisions.binding(task_id).route(snapshot.route_id)
    except Exception as exc:
        code = getattr(exc, "code", "MODEL_CONFIG_INVALID")
        raise B13ProviderBindingError(str(code)) from None

    gateway_kwargs: dict[str, Any] = {
        "snapshot": snapshot,
        "endpoint": route.base_url,
        "credential_resolver": _credential_callback(credential_resolver),
    }
    if transport is not None:
        gateway_kwargs["transport"] = transport
    else:
        gateway_kwargs["transport"] = _b13_yoli_transport
    return B13ProviderBinding(
        task_id=task_id,
        model_gateway=AgentModelGateway(**gateway_kwargs),
        tool_hosts=bind_b06p_tool_hosts(),
        snapshot=snapshot,
    )


def _decision(invocation: CapabilityInvocation, code: DenyCode) -> CapabilityDecision:
    return CapabilityDecision(
        outcome=DecisionOutcome.DENY,
        code=code,
        capability=invocation.capability,
        task_id=invocation.task_id,
        operation_key=invocation.operation_key,
        workspace_identity=invocation.workspace_identity,
        grant_id=invocation.grant.grant_id,
        grant_revision=invocation.grant.revision,
        policy_revision=invocation.grant.policy_revision,
    )


def _as_registry_receipt(receipt: Any) -> OperationReceipt:
    return OperationReceipt.model_validate(receipt.model_dump(by_alias=True))


def bind_b06p_tool_hosts(*, file_reader: FileReadHost | None = None) -> CapabilityHostRegistry:
    """Register the concrete B06P file host behind the shared registry."""

    reader = file_reader or FileReadHost()
    registry = CapabilityHostRegistry()

    def read(invocation: CapabilityInvocation, cancel: Any) -> HostOutcome:
        request = invocation.request.path
        if request is None or request.right.value != "read":
            decision = _decision(invocation, DenyCode.TOOL_PATH_AMBIGUOUS)
            return HostOutcome(
                None,
                decision,
                make_receipt(invocation, operation="path.read", decision=decision, result="denied"),
            )
        host_invocation = ToolInvocation(
            task_id=invocation.task_id,
            operation_key=invocation.operation_key,
            toolUseId=invocation.tool_use_id,
            grantRevision=invocation.grant_revision,
            policyRevision=invocation.grant.policy_revision,
            workspace_identity=invocation.workspace_identity,
        )
        context = HostContext(
            grant=invocation.grant,
            invocation=host_invocation,
            current_grant=lambda: invocation.grant,
            is_cancelled=cancel.is_cancelled,
        )
        result = reader.read_bytes(context, request.path, root_id=request.root_id)
        return HostOutcome(result.value, result.decision, _as_registry_receipt(result.receipt))

    registry.register(CapabilityName.PATH_READ.value, read)
    return registry


def make_b13_file_read_invocation(
    *,
    grant: Any,
    path: str,
    root_id: str,
    tool_use_id: str = "b13-tool-1",
) -> CapabilityInvocation:
    """Create a typed B06P read invocation for the controlled smoke path."""

    return CapabilityInvocation(
        grant=grant,
        request=CapabilityRequest(
            capability=CapabilityName.PATH_READ,
            binding=InvocationBinding(
                task_id=grant.task_id,
                operation_key=grant.operation_key,
                workspace_identity=grant.workspace_identity,
                policy_revision=grant.policy_revision,
            ),
            path=PathRequest(
                path=path,
                root_id=root_id,
                right=PermissionRight.READ,
                host_verified=False,
            ),
        ),
        toolUseId=tool_use_id,
        grantRevision=grant.revision,
    )


def make_b13_model_request(binding: B13ProviderBinding) -> AgentModelRequest:
    snapshot = binding.snapshot
    return AgentModelRequest(
        request_id="b13-provider-request",
        task_id=binding.task_id,
        operation_key="b13-provider-operation",
        purpose=snapshot.purpose,
        config_revision=snapshot.revision,
        route_id=snapshot.route_id,
        model=snapshot.model,
        system="Return one short acknowledgement.",
        messages=({"role": "user", "content": "B13 provider smoke"},),
        max_output_tokens=min(256, snapshot.limits.max_output_tokens),
    )


def create_b13_settings_smoke_binding() -> B13ProviderBinding:
    """Build a non-persistent smoke binding from the existing Settings source.

    This is deliberately a smoke-only adapter.  Production task bindings must
    use ``create_b13_provider_binding`` with the B05M model-runtime store and an
    injected credential resolver; this helper never becomes a runtime fallback.
    """

    from app.config import Settings
    from app.model_runtime.config import compile_snapshot

    settings = Settings()
    credential = settings.resolved_llm_main_api_key.strip()
    if not credential or credential.upper() == "EMPTY":
        raise B13ProviderBindingError(EXTERNAL_CREDENTIAL_PENDING)
    snapshot = compile_snapshot(
        {
            "schema_version": 1,
            "revision": 1,
            "activated_at": datetime.now(UTC),
            "routes": {
                "agent_main": {
                    "route_id": "settings-main",
                    "protocol": "openai_chat",
                    "base_url": settings.llm_main_base_url,
                    "credential_handle": "config://llm-main",
                    "model": settings.llm_main_model,
                    "fallback_route_ids": [],
                    "capabilities": {
                        "streaming": True,
                        "tool_use": True,
                        "parallel_tool_use": False,
                        "tool_choice": True,
                        "system_messages": True,
                        "usage_in_stream": True,
                        "prompt_cache": False,
                        "multimodal_images": False,
                        "multimodal_documents": False,
                    },
                    "limits": {
                        "context_window": 8_192,
                        "max_output_tokens": 256,
                        "request_timeout_s": 15.0,
                        "max_retries": 0,
                    },
                    "tokenizer": {
                        "kind": "conservative_estimate",
                        "identifier": "b13-settings-smoke",
                        "estimated": True,
                        "safety_margin_tokens": 8,
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
    return B13ProviderBinding(
        task_id="b13-live-smoke",
        model_gateway=AgentModelGateway(
            snapshot,
            endpoint=settings.llm_main_base_url,
            credential_resolver=lambda handle: credential if handle == "config://llm-main" else "",
            transport=_b13_yoli_transport,
        ),
        tool_hosts=bind_b06p_tool_hosts(),
        snapshot=snapshot,
    )


async def run_short_provider_smoke(
    binding: B13ProviderBinding,
    request: AgentModelRequest | None = None,
) -> B13SmokeResult:
    """Run one bounded model request and one controlled B06P receipt path."""

    try:
        events = 0
        terminal = False
        async for event in binding.model_gateway.stream(request or make_b13_model_request(binding)):
            events += 1
            if event.type == "error":
                code = str(event.payload.get("code") or "MODEL_PROVIDER_SMOKE_FAILED")
                if code in {
                    "MODEL_CREDENTIAL_MISSING",
                    "MODEL_CREDENTIAL_REVOKED",
                    "MODEL_CREDENTIAL_UNAVAILABLE",
                    "MODEL_AUTH_MISSING",
                    "MODEL_AUTH_INVALID",
                }:
                    return B13SmokeResult(EXTERNAL_CREDENTIAL_PENDING, EXTERNAL_CREDENTIAL_PENDING)
                return B13SmokeResult("FAIL", code, model_events=events)
            if event.type == "message_stop":
                terminal = True
    except Exception as exc:
        code = str(getattr(exc, "code", "MODEL_PROVIDER_SMOKE_FAILED"))
        if code in {
            EXTERNAL_CREDENTIAL_PENDING,
            "MODEL_CREDENTIAL_MISSING",
            "MODEL_CREDENTIAL_REVOKED",
            "MODEL_CREDENTIAL_UNAVAILABLE",
            "MODEL_AUTH_MISSING",
            "MODEL_AUTH_INVALID",
        }:
            return B13SmokeResult(EXTERNAL_CREDENTIAL_PENDING, EXTERNAL_CREDENTIAL_PENDING)
        return B13SmokeResult("FAIL", code)
    if not terminal:
        return B13SmokeResult("FAIL", "MODEL_STREAM_INCOMPLETE", model_events=events)
    return B13SmokeResult("PASS", "PROVIDER_STREAM_OK", model_events=events)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="B13 short provider smoke")
    parser.add_argument("--task-id", default="b13-live-smoke")
    parser.parse_args()
    try:
        binding = create_b13_settings_smoke_binding()
    except B13ProviderBindingError as exc:
        print(f"status={EXTERNAL_CREDENTIAL_PENDING} code={exc.code}")
        return 0
    result = asyncio.run(run_short_provider_smoke(binding))
    print(f"status={result.status} code={result.code} model_events={result.model_events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "B13_MODEL_GATEWAY_SOURCE",
    "B13_TOOL_HOST_SOURCE",
    "B13_YOLI_TRANSPORT_SHA",
    "EXTERNAL_CREDENTIAL_PENDING",
    "B13ProviderBinding",
    "B13ProviderBindingError",
    "B13SmokeResult",
    "bind_b06p_tool_hosts",
    "create_b13_provider_binding",
    "create_b13_settings_smoke_binding",
    "make_b13_file_read_invocation",
    "make_b13_model_request",
    "run_short_provider_smoke",
]
