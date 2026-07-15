"""不可变 ModelRuntimeSnapshot 和 request identity 边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from app.model_runtime.errors import (
    MODEL_CONFIG_STALE_REVISION,
    MODEL_REQUEST_IDENTITY_MISMATCH,
    ModelRuntimeRequestIdentityError,
    ModelRuntimeStaleRevisionError,
)
from app.model_runtime.types import (
    MODEL_RUNTIME_SCHEMA_VERSION,
    ModelCapabilities,
    ModelLimits,
    ModelProtocol,
    ModelPurpose,
    ReasoningPolicy,
    RequestIdentity,
    TokenizerPolicy,
    _FrozenModel,
)


class ModelRuntimeSnapshot(_FrozenModel):
    """一次 task/model 调用绑定的不可变、脱敏运行快照。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    schema_version: Literal[1] = Field(
        default=MODEL_RUNTIME_SCHEMA_VERSION,
        alias="schemaVersion",
    )
    revision: int = Field(ge=1)
    config_hash: str = Field(
        alias="configHash",
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    purpose: ModelPurpose
    route_id: str = Field(alias="routeId", min_length=1, max_length=64)
    protocol: ModelProtocol
    model: str = Field(min_length=1, max_length=256)
    capabilities: ModelCapabilities
    limits: ModelLimits
    tokenizer: TokenizerPolicy
    reasoning: ReasoningPolicy
    credential_handle: str = Field(alias="credentialHandle", repr=False)

    def public_dict(self) -> dict[str, object]:
        """返回可给 worker/log/diagnostics 的脱敏字段，不含 handle。"""

        return self.model_dump(mode="json", by_alias=True, exclude={"credential_handle"})

    def identity(self, *, request_id: str, task_id: str, operation_key: str) -> RequestIdentity:
        """以 snapshot revision/route 生成唯一 request identity。"""

        return RequestIdentity(
            requestId=request_id,
            taskId=task_id,
            operationKey=operation_key,
            configRevision=self.revision,
            routeId=self.route_id,
        )


def assert_snapshot_revision(snapshot: ModelRuntimeSnapshot, revision: int) -> None:
    """拒绝 stale revision；不在错误中暴露实际配置或秘密值。"""

    if revision != snapshot.revision:
        raise ModelRuntimeStaleRevisionError(MODEL_CONFIG_STALE_REVISION, field="revision")


def validate_request_identity(
    identity: RequestIdentity,
    snapshot: ModelRuntimeSnapshot,
) -> RequestIdentity:
    """确认 request identity 仍绑定同一 config revision 和 route。"""

    if (
        identity.config_revision != snapshot.revision
        or identity.route_id != snapshot.route_id
    ):
        raise ModelRuntimeRequestIdentityError(
            MODEL_REQUEST_IDENTITY_MISMATCH,
            field="config_revision_or_route_id",
        )
    return identity


__all__ = [
    "ModelRuntimeSnapshot",
    "assert_snapshot_revision",
    "validate_request_identity",
]
