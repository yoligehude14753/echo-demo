"""客户端 Bearer token 鉴权 + 每 token 滑动窗口限流。

设计：fail-closed —— token 白名单为空或不匹配一律 401；超过速率 429。
鉴权只校验客户端 token，绝不把客户端 token 透传给上游（上游用网关自己的真实凭证）。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import GatewaySettings


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


class RateLimiter:
    """进程内每 token 滑动窗口限流（单实例足够；多实例需换 Redis）。"""

    def __init__(self, window_s: float, max_requests: int) -> None:
        self._window_s = window_s
        self._max = max_requests
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, token: str) -> bool:
        now = time.monotonic()
        dq = self._hits[token]
        cutoff = now - self._window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= self._max:
            return False
        dq.append(now)
        return True


class Authenticator:
    """校验客户端 token + 限流。返回经过鉴权的 token（供日志/配额）。"""

    def __init__(self, settings: GatewaySettings) -> None:
        self._tokens = settings.token_set()
        self._limiter = RateLimiter(
            settings.rate_limit_window_s, settings.rate_limit_max_requests
        )

    def authenticate(self, request: Request) -> str:
        token = extract_bearer(request)
        if not token or token not in self._tokens:
            raise HTTPException(status_code=401, detail="invalid or missing api token")
        if not self._limiter.check(token):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return token
