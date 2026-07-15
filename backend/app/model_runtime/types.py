"""Model runtime 的纯数据合同。

本模块只描述配置和 request identity，不读取环境变量、用户设置或 provider SDK
的全局状态。所有模型均为 frozen Pydantic model；列表字段在边界内统一成 tuple，
避免 shallow-frozen model 留下可变嵌套值。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_RUNTIME_SCHEMA_VERSION = 1

ModelPurpose = Literal[
    "agent_main",
    "agent_compact",
    "agent_summary",
    "agent_quality",
    "chat",
    "minutes",
    "memory",
]
ModelProtocol = Literal["openai_chat", "anthropic_messages"]
TokenizerKind = Literal["provider", "local", "conservative_estimate"]
ReasoningMode = Literal["none", "hidden", "visible"]

RouteId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]

_MODEL_NAME_RE = re.compile(r"\S{1,256}\Z")
_OPAQUE_HANDLE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:(?://)?|(?:cred|handle)_)"
    r"[A-Za-z0-9._~:/-]{2,120}$"
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class ModelCapabilities(_FrozenModel):
    """Provider capability probe 的显式结果，不能从 model 名称推断。"""

    streaming: bool = Field(alias="streaming")
    tool_use: bool = Field(alias="toolUse")
    parallel_tool_use: bool = Field(alias="parallelToolUse")
    tool_choice: bool = Field(alias="toolChoice")
    system_messages: bool = Field(alias="systemMessages")
    usage_in_stream: bool = Field(alias="usageInStream")
    prompt_cache: bool = Field(alias="promptCache")
    multimodal_images: bool = Field(alias="multimodalImages")
    multimodal_documents: bool = Field(alias="multimodalDocuments")

    @model_validator(mode="after")
    def validate_combinations(self) -> ModelCapabilities:
        if self.parallel_tool_use and not self.tool_use:
            raise ValueError("parallel_tool_use requires tool_use")
        if self.tool_choice and not self.tool_use:
            raise ValueError("tool_choice requires tool_use")
        if self.usage_in_stream and not self.streaming:
            raise ValueError("usage_in_stream requires streaming")
        return self


class ModelLimits(_FrozenModel):
    context_window: int = Field(alias="contextWindow", ge=8_192, le=4_000_000)
    max_output_tokens: int = Field(alias="maxOutputTokens", ge=256, le=200_000)
    request_timeout_s: float = Field(alias="requestTimeoutS", gt=5.0, le=1_800.0)
    max_retries: int = Field(alias="maxRetries", ge=0, le=3)


class TokenizerPolicy(_FrozenModel):
    kind: TokenizerKind
    identifier: str = Field(min_length=1, max_length=256)
    estimated: bool
    safety_margin_tokens: int = Field(alias="safetyMarginTokens", ge=0, le=100_000)

    @field_validator("identifier")
    @classmethod
    def identifier_must_not_contain_control_chars(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("tokenizer identifier contains a control character")
        return value.strip()


class ReasoningPolicy(_FrozenModel):
    mode: ReasoningMode
    strip_think_tags: bool = Field(alias="stripThinkTags")
    token_budget: int | None = Field(default=None, alias="tokenBudget", ge=0, le=200_000)

    @model_validator(mode="after")
    def validate_combinations(self) -> ReasoningPolicy:
        if self.mode == "none" and self.token_budget not in (None, 0):
            raise ValueError("reasoning token budget requires a reasoning mode")
        return self


class ModelRoute(_FrozenModel):
    route_id: RouteId = Field(alias="routeId")
    protocol: ModelProtocol
    base_url: str = Field(
        alias="baseUrl",
        validation_alias=AliasChoices("base_url", "baseUrl", "endpoint"),
    )
    credential_handle: str = Field(alias="credentialHandle", repr=False)
    model: str = Field(min_length=1, max_length=256)
    fallback_route_ids: tuple[RouteId, ...] = Field(
        default_factory=tuple,
        alias="fallbackRouteIds",
    )
    capabilities: ModelCapabilities
    limits: ModelLimits
    tokenizer: TokenizerPolicy
    reasoning: ReasoningPolicy

    @field_validator("base_url")
    @classmethod
    def endpoint_must_be_public_http_url(cls, value: str) -> str:
        normalized = value.strip()
        parts = urlsplit(normalized)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("endpoint must be an http(s) URL without userinfo, query, or fragment")
        return normalized

    @field_validator("credential_handle")
    @classmethod
    def credential_handle_must_be_opaque(cls, value: str) -> str:
        normalized = value.strip()
        if not _OPAQUE_HANDLE_RE.fullmatch(normalized):
            raise ValueError("credential_handle must be an opaque reference")
        if normalized.lower().startswith(("http:", "https:")):
            raise ValueError("credential_handle must not be a provider URL")
        return normalized

    @field_validator("model")
    @classmethod
    def model_must_be_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not _MODEL_NAME_RE.fullmatch(normalized):
            raise ValueError("model must be a non-blank identifier")
        return normalized

    @model_validator(mode="after")
    def validate_reasoning_budget(self) -> ModelRoute:
        if (
            self.reasoning.token_budget is not None
            and self.reasoning.token_budget > self.limits.max_output_tokens
        ):
            raise ValueError("reasoning token budget exceeds max_output_tokens")
        return self


class ModelRuntimeConfig(_FrozenModel):
    """backend authoritative model config before a purpose is selected."""

    schema_version: Literal[1] = Field(
        default=MODEL_RUNTIME_SCHEMA_VERSION,
        alias="schemaVersion",
    )
    revision: int = Field(ge=1)
    routes: dict[ModelPurpose, ModelRoute] = Field(min_length=1)
    activated_at: datetime = Field(alias="activatedAt")
    # This is an optional producer-supplied assertion. The compiler recomputes
    # it from canonical non-secret fields and rejects a conflicting assertion.
    config_hash: str | None = Field(default=None, alias="configHash", repr=False)

    @model_validator(mode="after")
    def require_main_route(self) -> ModelRuntimeConfig:
        if "agent_main" not in self.routes:
            raise ValueError("agent_main route is required")
        return self


class RequestIdentity(_FrozenModel):
    """Identity carried by every model request and model event."""

    request_id: str = Field(alias="requestId", min_length=1, max_length=256)
    task_id: str = Field(alias="taskId", min_length=1, max_length=256)
    operation_key: str = Field(alias="operationKey", min_length=1, max_length=256)
    config_revision: int = Field(alias="configRevision", ge=1)
    route_id: RouteId = Field(alias="routeId")

    @field_validator("request_id", "task_id", "operation_key")
    @classmethod
    def identity_values_must_be_trimmed(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("request identity values must be non-blank")
        return normalized


__all__ = [
    "MODEL_RUNTIME_SCHEMA_VERSION",
    "ModelCapabilities",
    "ModelLimits",
    "ModelProtocol",
    "ModelPurpose",
    "ModelRoute",
    "ModelRuntimeConfig",
    "ReasoningPolicy",
    "RequestIdentity",
    "TokenizerPolicy",
]
