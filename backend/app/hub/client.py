"""Small HTTP client for the EchoDesk Hub endpoints."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from .state import HubDevice


class HubError(RuntimeError):
    """A public-safe Hub failure.

    The original exception is intentionally not kept on the object returned to
    the API layer so provider names, URLs, and response bodies cannot leak into
    user-visible errors.
    """

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PairingResult:
    code: str
    expires_at: str | None = None
    sync_token: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    sync_token: str | None = None


@dataclass(frozen=True, slots=True)
class SyncPushResult:
    applied: list[str]
    duplicate: list[str]
    conflict: list[str]


def _value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _cursor_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    return _text(value)


class HubClient:
    """Async client with no provider-specific protocol or crypto layer."""

    def __init__(
        self,
        base_url: str,
        *,
        device_id: str,
        sync_token: str | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.device_id = device_id
        self.sync_token = sync_token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "X-EchoDesk-Device-ID": device_id,
            },
        )

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        value = base_url.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise HubError("connection_failed")
        return value

    def set_sync_token(self, sync_token: str | None) -> None:
        self.sync_token = sync_token.strip() if sync_token else None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.sync_token:
            headers["X-Echo-Sync-Token"] = self.sync_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path.lstrip("/"),
                json=json_body,
                params=params,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise HubError("connection_failed", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise HubError("connection_failed", retryable=True) from exc

        if response.status_code >= 400:
            status_code = response.status_code
            if status_code in {401, 403}:
                code = "authentication_failed"
                retryable = False
            elif status_code == 409:
                code = "conflict"
                retryable = False
            elif status_code == 429 or status_code >= 500:
                code = "request_failed"
                retryable = True
            else:
                code = "request_failed"
                retryable = False
            raise HubError(code, status_code=status_code, retryable=retryable)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise HubError("invalid_response") from exc

    async def create_pairing(self) -> PairingResult:
        payload = await self._request(
            "POST",
            "/hub/v1/pairings",
            json_body={
                "device_id": self.device_id,
                "device_name": "EchoDesk Desktop",
                "platform": sys.platform,
            },
        )
        if not isinstance(payload, dict):
            raise HubError("invalid_response")
        nested = payload.get("pairing")
        pairing = nested if isinstance(nested, dict) else payload
        code = _text(_value(pairing, "pairing_code", "code", "manual_code", "pairingCode"))
        if not code:
            raise HubError("invalid_response")
        return PairingResult(
            code=code,
            expires_at=_text(_value(pairing, "expires_at", "expiresAt", "expiration")),
            sync_token=_text(_value(pairing, "sync_token", "token", "syncToken")),
        )

    async def claim_pairing(self, pairing_code: str) -> ClaimResult:
        code = pairing_code.strip()
        if not code:
            raise HubError("pairing_failed")
        payload = await self._request(
            "POST",
            "/hub/v1/pairings/claim",
            json_body={
                "pairing_code": code,
                "device_id": self.device_id,
                "device_name": "EchoDesk Desktop",
                "platform": sys.platform,
            },
        )
        if not isinstance(payload, dict):
            raise HubError("invalid_response")
        nested = payload.get("pairing")
        result = nested if isinstance(nested, dict) else payload
        return ClaimResult(sync_token=_text(_value(result, "sync_token", "token", "syncToken")))

    async def list_devices(self) -> list[HubDevice]:
        payload = await self._request("GET", "/hub/v1/devices")
        if isinstance(payload, list):
            raw_devices = payload
        elif isinstance(payload, dict):
            raw_devices = payload.get("devices") or payload.get("items") or []
        else:
            raise HubError("invalid_response")
        if not isinstance(raw_devices, list):
            raise HubError("invalid_response")
        devices: list[HubDevice] = []
        for raw_device in raw_devices:
            device = HubDevice.from_payload(raw_device)
            if device is not None:
                device.is_current = device.is_current or device.device_id == self.device_id
                devices.append(device)
        return devices

    async def revoke_device(self, device_id: str) -> None:
        value = device_id.strip()
        if not value or "/" in value or "\\" in value:
            raise HubError("request_failed")
        await self._request("DELETE", f"/hub/v1/devices/{quote(value, safe='')}")

    async def push(self, operations: list[dict[str, Any]]) -> SyncPushResult:
        if not operations:
            return SyncPushResult(applied=[], duplicate=[], conflict=[])
        applied: list[str] = []
        duplicate: list[str] = []
        conflict: list[str] = []

        for operation in operations:
            payload = await self._request(
                "POST",
                "/hub/v1/sync/push",
                json_body=operation,
            )
            if not isinstance(payload, dict):
                raise HubError("invalid_response")
            operation_id = _text(_value(payload, "operation_id", "operationId", "id"))
            if operation_id is None:
                operation_id = _text(_value(operation, "operation_id", "operationId", "id"))
            status = _text(payload.get("status"))
            if operation_id is None or status not in {"applied", "duplicate", "conflict"}:
                raise HubError("invalid_response")
            target = {
                "applied": applied,
                "duplicate": duplicate,
                "conflict": conflict,
            }[status]
            if operation_id not in target:
                target.append(operation_id)
        return SyncPushResult(applied=applied, duplicate=duplicate, conflict=conflict)

    async def changes(
        self,
        *,
        cursor: str | None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, str | int] = {"limit": max(1, min(limit, 500))}
        if cursor is not None and cursor.strip():
            params["cursor"] = cursor
        payload = await self._request(
            "GET",
            "/hub/v1/sync/changes",
            params=params,
        )
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)], cursor
        if not isinstance(payload, dict):
            raise HubError("invalid_response")
        raw_changes = payload.get("changes") or payload.get("items") or []
        if not isinstance(raw_changes, list):
            raise HubError("invalid_response")
        next_cursor = _cursor_text(_value(payload, "next_cursor", "nextCursor", "cursor"))
        return [item for item in raw_changes if isinstance(item, dict)], next_cursor

    async def snapshot(self) -> tuple[list[dict[str, Any]], str | None]:
        payload = await self._request("GET", "/hub/v1/sync/snapshot")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)], None
        if not isinstance(payload, dict):
            raise HubError("invalid_response")
        raw_entities: list[Any] | None = None
        for key in ("entities", "items", "changes"):
            candidate = payload.get(key)
            if isinstance(candidate, list) and candidate:
                raw_entities = candidate
                break
        if raw_entities is None:
            raw_entities = []
            for key in ("transcript_segments", "meeting_summaries", "memories"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    raw_entities.extend(candidate)
        return (
            [item for item in raw_entities if isinstance(item, dict)],
            _cursor_text(_value(payload, "cursor", "next_cursor", "nextCursor")),
        )

    def _events_url(self, cursor: str | None) -> str:
        parsed = urlsplit(self.base_url)
        query = urlencode({"cursor": cursor}) if cursor else ""
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit(
            (scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/hub/v1/sync/events", query, "")
        )

    async def listen_events(
        self,
        *,
        cursor: str | None,
        stop_event: asyncio.Event,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Listen until the runtime asks us to stop; callers reconnect on errors."""

        async with websockets.connect(
            self._events_url(cursor),
            extra_headers=self._headers(),
            open_timeout=self._client.timeout.connect,
        ) as socket:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict):
                            await on_event(item)
                elif isinstance(payload, dict):
                    change = payload.get("change")
                    await on_event(change if isinstance(change, dict) else payload)

    async def close(self) -> None:
        await self._client.aclose()
