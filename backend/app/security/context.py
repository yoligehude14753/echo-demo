"""请求 principal 上下文。

HTTP / WebSocket 边界完成服务端校验后绑定；业务与数据层只读取该值，不能从
客户端 body/query 自行构造 owner scope。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from app.security.models import Principal, local_principal

_principal_context: ContextVar[Principal | None] = ContextVar(
    "echodesk_principal",
    default=None,
)


def current_principal() -> Principal:
    return _principal_context.get() or local_principal()


def bind_principal(principal: Principal) -> Token[Principal | None]:
    return _principal_context.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _principal_context.reset(token)


__all__ = ["bind_principal", "current_principal", "reset_principal"]
