"""Single policy boundary for HTTP/WS identity and host capabilities."""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.config import OFFICIAL_ELECTRON_ORIGIN, Settings
from app.security.client_version import is_supported_public_client
from app.security.models import IssuedDeviceIdentity, IssuedSession, Principal, local_principal
from app.security.sessions import EnrollmentAdmissionPolicy, SessionStore

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_PUBLIC_METADATA_PATHS = frozenset({"/healthz", "/readyz", "/bootstrap"})
_ANONYMOUS_PUBLIC_SESSION_PATHS = frozenset(
    {"/session", "/session/enroll", "/session/renew", "/hub/v1/pairings/claim"}
)
_SESSION_BODY_ADMISSION_PATHS = frozenset(
    {
        "/session",
        "/session/enroll",
        "/session/renew",
        "/session/credential/rotate",
        "/hub/v1/pairings/claim",
    }
)
_SYNC_HUB_PATH_PREFIX = "/hub/v1/"
_LAN_SAFE_GET_PATTERNS = (
    re.compile(r"^/healthz$"),
    re.compile(r"^/readyz$"),
    re.compile(r"^/meetings/[^/]+/share$"),
    re.compile(r"^/meetings/[^/]+/minutes\.md$"),
    re.compile(r"^/artifacts/[^/]+/download$"),
)
_SHARE_TARGET_PATTERNS = (
    ("meeting", re.compile(r"^/meetings/([^/]+)/(?:share|minutes\.md)$")),
    ("artifact", re.compile(r"^/artifacts/([^/]+)/download$")),
)
_HOST_CAPABILITY_PATTERNS = (
    (None, re.compile(r"^/admin(?:/.*)?$")),
    (None, re.compile(r"^/healthz/full$")),
    (None, re.compile(r"^/workspace(?:/.*)?$")),
    ("POST", re.compile(r"^/artifacts/generate$")),
    ("POST", re.compile(r"^/agents/tasks$")),
    ("POST", re.compile(r"^/agents/tasks/[^/]+/(?:cancel|retry)$")),
    ("POST", re.compile(r"^/agents/grants/claude_code$")),
    ("DELETE", re.compile(r"^/agents/grants/[^/]+$")),
)


class AccessPolicyError(RuntimeError):
    """An HTTP/WS request failed the centralized access policy."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class PreAuthAdmissionError(AccessPolicyError):
    """A request was rejected before any principal lookup was attempted."""

    def __init__(self, channel: str, reason: str, *, retry_after_s: int = 1) -> None:
        super().__init__(f"pre-auth {channel} {reason} exceeded", status_code=429)
        self.channel = channel
        self.reason = reason
        self.retry_after_s = max(1, retry_after_s)


@dataclass(slots=True)
class _AdmissionPeer:
    attempts: deque[float]
    active: int
    last_seen: float


class _PreAuthAdmissionLease:
    def __init__(self, gate: PreAuthAdmission, peer_key: str) -> None:
        self._gate = gate
        self._peer_key = peer_key
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release(self._peer_key)

    async def __aenter__(self) -> _PreAuthAdmissionLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()


class PreAuthAdmission:
    """Bounded process-local admission before token or ticket validation.

    The gate deliberately combines a global ceiling with per-peer ceilings.
    Rate windows bound fast authentication failures; active leases bound slow
    SQLite lookups and WebSocket first-frame handshakes.
    """

    def __init__(
        self,
        *,
        channel: str,
        global_concurrent: int,
        peer_concurrent: int,
        global_attempts: int,
        peer_attempts: int,
        window_s: float,
        max_peers: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not channel.strip()
            or global_concurrent < 1
            or peer_concurrent < 1
            or global_attempts < 1
            or peer_attempts < 1
            or window_s <= 0
            or max_peers < 1
        ):
            raise ValueError("pre-auth admission bounds are invalid")
        self.channel = channel.strip()
        self.global_concurrent = global_concurrent
        self.peer_concurrent = peer_concurrent
        self.global_attempts = global_attempts
        self.peer_attempts = peer_attempts
        self.window_s = window_s
        self.max_peers = max_peers
        self._clock = clock
        self._global_window: deque[float] = deque()
        self._global_active = 0
        self._peers: OrderedDict[str, _AdmissionPeer] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _trim(window: deque[float], cutoff: float) -> None:
        while window and window[0] <= cutoff:
            window.popleft()

    def _retry_after(self, window: deque[float], now: float) -> int:
        if not window:
            return 1
        return int(max(1.0, window[0] + self.window_s - now))

    def _peer_locked(self, peer_key: str, now: float) -> _AdmissionPeer:
        peer = self._peers.pop(peer_key, None)
        if peer is not None:
            self._peers[peer_key] = peer
            return peer
        if len(self._peers) >= self.max_peers:
            evict_key = next(
                (key for key, candidate in self._peers.items() if candidate.active == 0),
                None,
            )
            if evict_key is None:
                raise PreAuthAdmissionError(self.channel, "capacity")
            self._peers.pop(evict_key)
        peer = _AdmissionPeer(deque(), 0, now)
        self._peers[peer_key] = peer
        return peer

    async def acquire(self, peer_key: str) -> _PreAuthAdmissionLease:
        key = peer_key.strip() or "unknown"
        async with self._lock:
            now = self._clock()
            cutoff = now - self.window_s
            self._trim(self._global_window, cutoff)
            peer = self._peer_locked(key, now)
            self._trim(peer.attempts, cutoff)
            peer.last_seen = now

            if len(self._global_window) >= self.global_attempts:
                raise PreAuthAdmissionError(
                    self.channel,
                    "rate limit",
                    retry_after_s=self._retry_after(self._global_window, now),
                )
            if len(peer.attempts) >= self.peer_attempts:
                raise PreAuthAdmissionError(
                    self.channel,
                    "rate limit",
                    retry_after_s=self._retry_after(peer.attempts, now),
                )

            # Capacity rejections still consume rate budget so a busy gate
            # cannot be hammered with unmetered retries.
            self._global_window.append(now)
            peer.attempts.append(now)
            if self._global_active >= self.global_concurrent or peer.active >= self.peer_concurrent:
                raise PreAuthAdmissionError(self.channel, "capacity")

            self._global_active += 1
            peer.active += 1
        return _PreAuthAdmissionLease(self, key)

    async def _release(self, peer_key: str) -> None:
        async with self._lock:
            peer = self._peers.get(peer_key)
            if peer is None or peer.active < 1:
                return
            peer.active -= 1
            peer.last_seen = self._clock()
            self._global_active = max(0, self._global_active - 1)

    @property
    def tracked_peers(self) -> int:
        return len(self._peers)


class SessionIssueRateLimitError(AccessPolicyError):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("session issuance rate limit exceeded", status_code=429)
        self.retry_after_s = max(1, retry_after_s)


@dataclass(slots=True)
class _IssueWindow:
    attempts: deque[float]
    last_seen: float


class SessionIssueLimiter:
    """Bounded, process-local fixed-window limiter keyed by transport peer."""

    def __init__(
        self,
        *,
        max_requests: int = 12,
        window_s: float = 60.0,
        max_clients: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1 or window_s <= 0 or max_clients < 1:
            raise ValueError("session issue limiter bounds must be positive")
        self.max_requests = max_requests
        self.window_s = window_s
        self.max_clients = max_clients
        self._clock = clock
        self._clients: OrderedDict[str, _IssueWindow] = OrderedDict()

    def check(self, client_key: str) -> None:
        now = self._clock()
        key = client_key.strip() or "unknown"
        window = self._clients.pop(key, None)
        if window is None:
            while len(self._clients) >= self.max_clients:
                self._clients.popitem(last=False)
            window = _IssueWindow(deque(), now)
        cutoff = now - self.window_s
        while window.attempts and window.attempts[0] <= cutoff:
            window.attempts.popleft()
        if len(window.attempts) >= self.max_requests:
            self._clients[key] = window
            retry_after = int(max(1.0, window.attempts[0] + self.window_s - now))
            raise SessionIssueRateLimitError(retry_after)
        window.attempts.append(now)
        window.last_seen = now
        self._clients[key] = window

    @property
    def tracked_clients(self) -> int:
        return len(self._clients)


class AccessPolicy:
    """Authoritative policy for request identity and host-runtime authorization."""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionStore,
        *,
        session_limiter: SessionIssueLimiter | None = None,
        sensitive_limiter: SessionIssueLimiter | None = None,
        http_admission: PreAuthAdmission | None = None,
        session_body_admission: PreAuthAdmission | None = None,
        websocket_admission: PreAuthAdmission | None = None,
        cleanup_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.session_limiter = session_limiter or SessionIssueLimiter()
        self.sensitive_limiter = sensitive_limiter or SessionIssueLimiter(
            max_requests=6,
            window_s=60,
        )
        self.http_admission = http_admission or PreAuthAdmission(
            channel="http",
            global_concurrent=settings.preauth_http_global_concurrent,
            peer_concurrent=settings.preauth_http_peer_concurrent,
            global_attempts=settings.preauth_http_global_requests_per_window,
            peer_attempts=settings.preauth_http_peer_requests_per_window,
            window_s=settings.preauth_window_s,
            max_peers=settings.preauth_max_peers,
            clock=clock,
        )
        # Session credential bodies are intentionally separated from the
        # short token-lookup gate. A slow anonymous enrollment must retain a
        # peer lease until its response finishes, but that lease must not make
        # normal authenticated traffic from the same NAT wait. When the body
        # pool has more than one slot, cap one peer below the pool size so a
        # single source cannot reserve every global body slot.
        session_body_global_concurrent = min(
            settings.preauth_http_global_concurrent,
            settings.upload_global_concurrent_requests,
        )
        session_body_peer_concurrent = min(
            settings.preauth_http_peer_concurrent,
            max(1, session_body_global_concurrent - 1),
        )
        self.session_body_admission = session_body_admission or PreAuthAdmission(
            channel="session body",
            global_concurrent=session_body_global_concurrent,
            peer_concurrent=session_body_peer_concurrent,
            global_attempts=settings.preauth_http_global_requests_per_window,
            peer_attempts=settings.preauth_http_peer_requests_per_window,
            window_s=settings.preauth_window_s,
            max_peers=settings.preauth_max_peers,
            clock=clock,
        )
        self.websocket_admission = websocket_admission or PreAuthAdmission(
            channel="websocket",
            global_concurrent=settings.preauth_ws_global_concurrent,
            peer_concurrent=settings.preauth_ws_peer_concurrent,
            global_attempts=settings.preauth_ws_global_attempts_per_window,
            peer_attempts=settings.preauth_ws_peer_attempts_per_window,
            window_s=settings.preauth_window_s,
            max_peers=settings.preauth_max_peers,
            clock=clock,
        )
        self.enrollment_admission_policy = EnrollmentAdmissionPolicy(
            window_s=settings.enrollment_admission_window_s,
            peer_max_per_window=settings.enrollment_admission_peer_max_per_window,
            global_max_per_window=settings.enrollment_admission_global_max_per_window,
            peer_max_per_day=settings.enrollment_admission_peer_max_per_day,
            global_max_per_day=settings.enrollment_admission_global_max_per_day,
            total_active_max=settings.enrollment_admission_total_active_max,
            cleanup_batch_size=settings.enrollment_admission_cleanup_batch_size,
        )
        self._cleanup_interval_s = max(0.0, cleanup_interval_s)
        self._clock = clock
        self._last_cleanup_at: float | None = None
        self._issue_lock = asyncio.Lock()

    @staticmethod
    def client_host(client: object | None) -> str:
        host = getattr(client, "host", None)
        return str(host) if host else "unknown"

    def require_allowed_origin(
        self,
        origins: Sequence[str],
        *,
        client_host: str,
    ) -> None:
        """Reject any explicit browser Origin that is not a unique allowlist match.

        Native and server-to-server clients commonly omit Origin; absence remains
        valid. Explicit blank, ``null`` and duplicate values are rejected so they
        cannot become a cross-site bypass. The packaged Electron scheme is accepted
        only at its exact, product-owned origin.
        """

        values = tuple(origins)
        if not values:
            return
        if len(values) != 1:
            raise AccessPolicyError("origin not allowed", status_code=403)
        origin = values[0].strip()
        if not origin or origin.lower() == "null" or "," in origin:
            raise AccessPolicyError("origin not allowed", status_code=403)
        if origin.lower().startswith("echodesk:"):
            if origin == OFFICIAL_ELECTRON_ORIGIN:
                return
            raise AccessPolicyError("origin not allowed", status_code=403)
        if origin == "file://":
            if (
                self.settings.electron_file_origin_enabled
                and not self.settings.public_demo_mode
                and client_host.lower() in _LOOPBACK_HOSTS
            ):
                return
            raise AccessPolicyError("origin not allowed", status_code=403)
        allowed = self.settings.allowed_origins_list
        if "*" not in allowed and origin not in allowed:
            raise AccessPolicyError("origin not allowed", status_code=403)

    async def admit_http(self, client_key: str) -> _PreAuthAdmissionLease | None:
        if not self.settings.public_demo_mode:
            return None
        return await self.http_admission.acquire(client_key)

    @staticmethod
    def is_session_body_admission_route(method: str, path: str) -> bool:
        return method.upper() == "POST" and path in _SESSION_BODY_ADMISSION_PATHS

    async def admit_session_body(
        self,
        *,
        method: str,
        path: str,
        client_key: str,
    ) -> _PreAuthAdmissionLease | None:
        if not self.settings.public_demo_mode or not self.is_session_body_admission_route(
            method,
            path,
        ):
            return None
        return await self.session_body_admission.acquire(client_key)

    async def admit_websocket(self, client_key: str) -> _PreAuthAdmissionLease | None:
        if not self.settings.public_demo_mode:
            return None
        return await self.websocket_admission.acquire(client_key)

    @staticmethod
    def share_target(path: str) -> tuple[str, str] | None:
        for resource_type, pattern in _SHARE_TARGET_PATTERNS:
            match = pattern.fullmatch(path)
            if match:
                return resource_type, match.group(1)
        return None

    @staticmethod
    def is_host_capability_route(method: str, path: str) -> bool:
        normalized = method.upper()
        return any(
            (required_method is None or required_method == normalized) and pattern.fullmatch(path)
            for required_method, pattern in _HOST_CAPABILITY_PATTERNS
        )

    def is_lan_request_allowed(self, *, method: str, path: str, client_host: str) -> bool:
        if self.settings.public_demo_mode:
            # Public deployments are intentionally reached through a trusted
            # reverse proxy, which restores the remote peer address.  The LAN
            # share gate is not an authentication layer; public requests are
            # constrained by the version, origin, session, ownership and
            # host-admin policy below instead.
            return True
        return (
            self.settings.lan_full_api_enabled
            or client_host in _LOOPBACK_HOSTS
            or method.upper() == "OPTIONS"
            or (
                method.upper() == "GET"
                and any(pattern.fullmatch(path) for pattern in _LAN_SAFE_GET_PATTERNS)
            )
        )

    def _has_admin_token(self, authorization: str, x_echo_admin_token: str) -> bool:
        expected = self.settings.debug_token.strip()
        if not expected:
            return False
        if x_echo_admin_token == expected:
            return True
        scheme, _, token = authorization.partition(" ")
        return scheme.lower() == "bearer" and token == expected

    def require_host_admin(
        self,
        *,
        client_host: str,
        authorization: str = "",
        x_echo_admin_token: str = "",
    ) -> None:
        if not self.settings.public_demo_mode and client_host in _LOOPBACK_HOSTS:
            return
        if self._has_admin_token(authorization, x_echo_admin_token):
            return
        raise AccessPolicyError("host-admin authorization required", status_code=403)

    async def resolve_http_principal(  # noqa: PLR0911 - ordered auth boundary
        self,
        *,
        method: str,
        path: str,
        client_host: str,
        authorization: str = "",
        x_echo_admin_token: str = "",
        share_token: str = "",
        client_version: str = "",
        sync_token: str = "",
    ) -> Principal:
        if self.is_host_capability_route(method, path):
            self.require_host_admin(
                client_host=client_host,
                authorization=authorization,
                x_echo_admin_token=x_echo_admin_token,
            )
            return local_principal()
        if path.startswith(_SYNC_HUB_PATH_PREFIX) and sync_token.strip():
            return await self.sessions.validate_sync_token(sync_token)
        if not self.settings.public_demo_mode:
            return local_principal()
        if method.upper() == "OPTIONS" or path in _PUBLIC_METADATA_PATHS:
            return local_principal()
        if self._has_admin_token(authorization, x_echo_admin_token):
            return local_principal()
        share_target = self.share_target(path)
        if method.upper() == "GET" and share_target is not None and share_token:
            return await self.sessions.validate_resource_ticket(
                share_token,
                resource_type=share_target[0],
                resource_id=share_target[1],
            )
        if not is_supported_public_client(client_version):
            raise AccessPolicyError("client upgrade required", status_code=426)
        principal = local_principal()
        if path not in _ANONYMOUS_PUBLIC_SESSION_PATHS:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise AccessPolicyError("session required", status_code=401)
            principal = await self.sessions.validate_public_token(token)
        return principal

    async def resolve_websocket_principal(
        self,
        *,
        client_host: str,
        path: str,
        authorization: str = "",
        query_token: str = "",
    ) -> Principal:
        if not self.settings.public_demo_mode:
            if not self.is_lan_request_allowed(
                method="GET",
                path=path,
                client_host=client_host,
            ):
                raise AccessPolicyError("LAN websocket access disabled", status_code=403)
            return local_principal()
        scheme, _, header_token = authorization.partition(" ")
        token = header_token if scheme.lower() == "bearer" else query_token
        return await self.sessions.validate_public_token(token)

    async def issue_public_session(
        self,
        *,
        client_key: str,
        enrollment_id: str,
        device_secret: str,
    ) -> IssuedSession:
        enrolled = await self.enroll_public_device(
            client_key=client_key,
            enrollment_id=enrollment_id,
            device_secret=device_secret,
        )
        return enrolled.session

    async def enroll_public_device(
        self,
        *,
        client_key: str,
        enrollment_id: str,
        device_secret: str,
        display_name: str | None = None,
    ) -> IssuedDeviceIdentity:
        async with self._issue_lock:
            self.session_limiter.check(client_key)
            now = self._clock()
            if (
                self._last_cleanup_at is None
                or now - self._last_cleanup_at >= self._cleanup_interval_s
            ):
                await self.sessions.cleanup_expired_sessions()
                self._last_cleanup_at = now
            return await self.sessions.enroll_public_device(
                enrollment_id=enrollment_id,
                device_secret=device_secret,
                peer_key=client_key,
                display_name=display_name,
                admission_policy=self.enrollment_admission_policy,
            )

    async def renew_public_session(
        self,
        *,
        client_key: str,
        device_credential: str,
    ) -> IssuedSession:
        async with self._issue_lock:
            self.session_limiter.check(client_key)
            return await self.sessions.renew_public_session(device_credential)

    def check_sensitive_action(
        self,
        *,
        client_key: str,
        principal: Principal,
        action: str,
    ) -> None:
        family = principal.family_id or principal.session_id
        self.sensitive_limiter.check(f"{action}:{client_key}:{family}")


__all__ = [
    "AccessPolicy",
    "AccessPolicyError",
    "PreAuthAdmission",
    "PreAuthAdmissionError",
    "SessionIssueLimiter",
    "SessionIssueRateLimitError",
]
