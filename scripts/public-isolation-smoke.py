#!/usr/bin/env python3
"""EchoDesk 公网模式双身份隔离 smoke。

此脚本只调用正式公网 API，不读取数据库、不使用管理员凭据，也不会触发 LLM：

* 两套身份分别执行 enrollment 与 device credential renewal；
* A 创建会议和转写片段，B 对已知 A meeting id 的读、写、清理、结束、finalize
  都必须返回 404；
* A 上传一个纯文本 RAG 文档，以本地解析生成 workflow，随后验证 RAG、workflow
  与 meeting artifacts 集合均按 owner 隔离；
* A、B 同时建立 WebSocket，A 的事件只能进入 A 的 stream；
* public bearer 不能调用 host-admin 能力；
* 最后删除 RAG 文档、清理会议 outputs、结束会议并撤销两套 session family。

运行时输出是 JSON Lines，字段值只包含固定检查名、状态码、布尔值和随机 smoke id。
脚本从不输出 bearer、device credential、服务端资源 id、响应正文或转写正文。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit

import httpx

_SMOKE_ID_RE = re.compile(r"\A[0-9]{8}t[0-9]{6}z-[a-f0-9]{10}\Z")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_JSON_LIMIT_BYTES = 2 * 1024 * 1024
_WS_FRAME_LIMIT_BYTES = 128 * 1024
_DEPLOYMENT_GATE_HEADER = "X-Echo-Deployment-Gate"
_CLIENT_VERSION_HEADER = "X-EchoDesk-Client-Version"
_DEPLOYMENT_GATE_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{43,128}\Z")

# 调用方可能预先打开全局 DEBUG；WebSocket debug 会记录含 bearer 的 client_hello。
for _logger_name in ("httpx", "httpcore", "websockets", "websockets.client"):
    logging.getLogger(_logger_name).disabled = True
_WS_LOGGER = logging.getLogger("echodesk.public_isolation_smoke.websocket")
_WS_LOGGER.disabled = True


class SmokeFailure(RuntimeError):
    """Sanitized failure carrying no response body or credential."""

    def __init__(self, check: str, *, status: int | None = None) -> None:
        super().__init__(check)
        self.check = check
        self.status = status


@dataclass(slots=True)
class ResultSink:
    smoke_id: str
    stream: TextIO = sys.stdout

    def emit(self, check: str, *, ok: bool, status: int | None = None) -> None:
        record: dict[str, str | int | bool] = {
            "smoke_id": self.smoke_id,
            "check": check,
            "ok": ok,
        }
        if status is not None:
            record["status"] = status
        print(json.dumps(record, ensure_ascii=True, sort_keys=True), file=self.stream, flush=True)


@dataclass(frozen=True, slots=True)
class EnrollmentMaterial:
    enrollment_id: str = field(repr=False)
    device_credential: str = field(repr=False)

    @classmethod
    def create(cls, smoke_id: str, label: str) -> EnrollmentMaterial:
        return cls(
            enrollment_id=f"isolation-{smoke_id}-{label}-{secrets.token_urlsafe(32)}",
            device_credential=f"isolation-device-{label}-{secrets.token_urlsafe(40)}",
        )


@dataclass(frozen=True, slots=True)
class IssuedIdentity:
    token: str = field(repr=False)
    tenant_id: str = field(repr=False)
    owner_id: str = field(repr=False)
    device_id: str = field(repr=False)

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.tenant_id, self.owner_id, self.device_id


def _new_smoke_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()
    return f"{stamp}-{secrets.token_hex(5)}"


def _normalize_base_url(raw: str, *, allow_insecure_http: bool) -> str:
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("base URL must not contain an API path prefix")
    if (
        parsed.scheme == "http"
        and parsed.hostname.lower() not in _LOOPBACK_HOSTS
        and not allow_insecure_http
    ):
        raise ValueError("non-loopback HTTP requires --allow-insecure-http")
    netloc = parsed.netloc
    return urlunsplit(SplitResult(parsed.scheme, netloc, "", "", ""))


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit(SplitResult(scheme, parsed.netloc, "/ws/echo", "", ""))


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _object(value: object, check: str, sink: ResultSink, *, status: int) -> dict[str, Any]:
    ok = isinstance(value, dict)
    sink.emit(check, ok=ok, status=status)
    if not ok:
        raise SmokeFailure(check, status=status)
    return cast(dict[str, Any], value)


def _list(value: object, check: str, sink: ResultSink, *, status: int) -> list[Any]:
    ok = isinstance(value, list)
    sink.emit(check, ok=ok, status=status)
    if not ok:
        raise SmokeFailure(check, status=status)
    return cast(list[Any], value)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_response(response: httpx.Response, check: str, sink: ResultSink) -> object:
    content_length = len(response.content)
    if content_length > _JSON_LIMIT_BYTES:
        sink.emit(check, ok=False, status=response.status_code)
        raise SmokeFailure(check, status=response.status_code)
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        sink.emit(check, ok=False, status=response.status_code)
        raise SmokeFailure(check, status=response.status_code) from None


class IsolationSmoke:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        sink: ResultSink,
        deployment_gate_token: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.sink = sink
        self.meeting_id = f"isolation-{sink.smoke_id}-a"
        self.segment_text = f"EchoDesk public isolation smoke {sink.smoke_id}"
        self._tokens: dict[str, str] = {}
        self._meeting_started = False
        self._rag_doc_id: str | None = None
        self._cleanup_failed = False
        headers = {
            "User-Agent": "EchoDesk-Public-Isolation-Smoke/0.3.2",
            _CLIENT_VERSION_HEADER: "0.3.2",
        }
        if deployment_gate_token is not None:
            headers[_DEPLOYMENT_GATE_HEADER] = deployment_gate_token
        self._deployment_gate_token = deployment_gate_token
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            headers=headers,
        )

    async def _request(
        self,
        check: str,
        method: str,
        path: str,
        *,
        token: str | None = None,
        expected: frozenset[int],
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self.client.request(method, path, headers=headers, **kwargs)
        except (httpx.HTTPError, TimeoutError):
            self.sink.emit(check, ok=False, status=0)
            raise SmokeFailure(check, status=0) from None
        ok = response.status_code in expected
        self.sink.emit(check, ok=ok, status=response.status_code)
        if not ok:
            raise SmokeFailure(check, status=response.status_code)
        return response

    def _check(self, check: str, condition: bool, *, status: int | None = None) -> None:
        self.sink.emit(check, ok=condition, status=status)
        if not condition:
            raise SmokeFailure(check, status=status)

    async def _enroll(self, label: str) -> IssuedIdentity:
        material = EnrollmentMaterial.create(self.sink.smoke_id, label)
        enrolled_response = await self._request(
            f"identity_{label}_enroll",
            "POST",
            "/session/enroll",
            expected=frozenset({201}),
            json={
                "enrollment_id": material.enrollment_id,
                "device_secret": material.device_credential,
                "display_name": f"isolation-smoke-{label}",
            },
        )
        enrolled = _object(
            _json_response(enrolled_response, f"identity_{label}_enroll_json", self.sink),
            f"identity_{label}_enroll_shape",
            self.sink,
            status=enrolled_response.status_code,
        )
        initial_token, initial_scope = self._parse_session(enrolled)
        self._tokens[label] = initial_token

        renewed_response = await self._request(
            f"identity_{label}_renew",
            "POST",
            "/session/renew",
            expected=frozenset({200}),
            json={"device_credential": material.device_credential},
        )
        renewed = _object(
            _json_response(renewed_response, f"identity_{label}_renew_json", self.sink),
            f"identity_{label}_renew_shape",
            self.sink,
            status=renewed_response.status_code,
        )
        token, scope = self._parse_session(renewed)
        self._tokens[label] = token
        self._check(f"identity_{label}_device_continuity", scope == initial_scope)
        self._check(f"identity_{label}_session_rotated", token != initial_token)
        return IssuedIdentity(token, *scope)

    @staticmethod
    def _parse_session(payload: dict[str, Any]) -> tuple[str, tuple[str, str, str]]:
        token = _string(payload.get("token"))
        principal = payload.get("principal")
        if token is None or not isinstance(principal, dict):
            raise SmokeFailure("identity_session_contract")
        tenant_id = _string(principal.get("tenant_id"))
        owner_id = _string(principal.get("owner_id"))
        device_id = _string(principal.get("device_id"))
        if tenant_id is None or owner_id is None or device_id is None:
            raise SmokeFailure("identity_session_contract")
        return token, (tenant_id, owner_id, device_id)

    async def _recv_ws_object(self, websocket: Any) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.timeout_s)
        except (TimeoutError, ConnectionError, RuntimeError):
            raise SmokeFailure("websocket_receive") from None
        if isinstance(raw, bytes):
            if len(raw) > _WS_FRAME_LIMIT_BYTES:
                raise SmokeFailure("websocket_frame_bound")
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise SmokeFailure("websocket_frame_encoding") from None
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _WS_FRAME_LIMIT_BYTES:
            raise SmokeFailure("websocket_frame_bound")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise SmokeFailure("websocket_frame_json") from None
        if not isinstance(value, dict):
            raise SmokeFailure("websocket_frame_shape")
        return value

    async def _ws_hello(self, websocket: Any, identity: IssuedIdentity) -> dict[str, Any]:
        await websocket.send(
            json.dumps(
                {
                    "type": "client_hello",
                    "last_seq": 0,
                    "client_version": "0.3.2+public-isolation-no-replay",
                    "auth": {"type": "bearer", "token": identity.token},
                },
                separators=(",", ":"),
            )
        )
        frame = await self._recv_ws_object(websocket)
        if frame.get("type") != "server_hello":
            raise SmokeFailure("websocket_hello")
        return frame

    async def _websocket_isolation(
        self,
        identity_a: IssuedIdentity,
        identity_b: IssuedIdentity,
    ) -> None:
        try:
            import websockets
        except ImportError:
            self.sink.emit("websocket_dependency", ok=False)
            raise SmokeFailure("websocket_dependency") from None

        ws_url = _websocket_url(self.base_url)
        connect_options: dict[str, Any] = {
            "open_timeout": self.timeout_s,
            "close_timeout": self.timeout_s,
            "max_size": _WS_FRAME_LIMIT_BYTES,
            "ping_interval": None,
            "logger": _WS_LOGGER,
        }
        if self._deployment_gate_token is not None:
            connect_options["extra_headers"] = {
                _DEPLOYMENT_GATE_HEADER: self._deployment_gate_token
            }
        try:
            async with (
                websockets.connect(ws_url, **connect_options) as ws_a,
                websockets.connect(ws_url, **connect_options) as ws_b,
            ):
                hello_a = await self._ws_hello(ws_a, identity_a)
                hello_b = await self._ws_hello(ws_b, identity_b)
                self._check("websocket_pair_connected", bool(hello_a and hello_b))

                started = await self._request(
                    "meeting_a_start",
                    "POST",
                    f"/meetings/{self.meeting_id}/start",
                    token=identity_a.token,
                    expected=frozenset({200}),
                )
                self._meeting_started = started.status_code == 200

                event_a: dict[str, Any] | None = None
                for _ in range(16):
                    candidate = await self._recv_ws_object(ws_a)
                    if candidate.get("type") == "meeting.started":
                        event_a = candidate
                        break
                    if candidate.get("type") != "server_ping":
                        raise SmokeFailure("websocket_a_unexpected_frame")
                self._check(
                    "websocket_a_receives_own_event",
                    event_a is not None and event_a.get("meeting_id") == self.meeting_id,
                )

                await ws_b.send('{"type":"client_ping"}')
                frame_b = await self._recv_ws_object(ws_b)
                self._check("websocket_b_receives_no_a_event", frame_b.get("type") == "server_ping")
                payload_b = frame_b.get("payload")
                self._check(
                    "websocket_b_stream_is_empty",
                    isinstance(payload_b, dict) and payload_b.get("max_seq") == 0,
                )

            async with websockets.connect(ws_url, **connect_options) as ws_b_fence:
                hello_b_fence = await self._ws_hello(ws_b_fence, identity_b)
                payload = hello_b_fence.get("payload")
                self._check(
                    "websocket_b_reconnect_fence_is_empty",
                    isinstance(payload, dict) and payload.get("max_seq") == 0,
                )
        except SmokeFailure:
            raise
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            self.sink.emit("websocket_transport", ok=False)
            raise SmokeFailure("websocket_transport") from None

    async def _meeting_isolation(
        self,
        identity_a: IssuedIdentity,
        identity_b: IssuedIdentity,
    ) -> None:
        injected = await self._request(
            "meeting_a_write",
            "POST",
            f"/meetings/{self.meeting_id}/inject_segment",
            token=identity_a.token,
            expected=frozenset({200}),
            json={"text": self.segment_text, "start_ms": 0, "end_ms": 1000},
        )
        _object(
            _json_response(injected, "meeting_a_write_json", self.sink),
            "meeting_a_write_shape",
            self.sink,
            status=injected.status_code,
        )

        own_read = await self._request(
            "meeting_a_read",
            "GET",
            f"/meetings/{self.meeting_id}/transcript",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        own_segments = _list(
            _json_response(own_read, "meeting_a_read_json", self.sink),
            "meeting_a_read_shape",
            self.sink,
            status=own_read.status_code,
        )
        self._check(
            "meeting_a_read_content",
            len(own_segments) == 1
            and isinstance(own_segments[0], dict)
            and own_segments[0].get("text") == self.segment_text,
            status=own_read.status_code,
        )

        b_meetings_response = await self._request(
            "meeting_b_list",
            "GET",
            "/meetings",
            token=identity_b.token,
            expected=frozenset({200}),
        )
        b_meetings = _list(
            _json_response(b_meetings_response, "meeting_b_list_json", self.sink),
            "meeting_b_list_shape",
            self.sink,
            status=b_meetings_response.status_code,
        )
        self._check(
            "meeting_b_list_excludes_a",
            all(
                not isinstance(row, dict) or row.get("meeting_id") != self.meeting_id
                for row in b_meetings
            ),
            status=b_meetings_response.status_code,
        )

        await self._request(
            "meeting_b_read_known_a",
            "GET",
            f"/meetings/{self.meeting_id}/transcript",
            token=identity_b.token,
            expected=frozenset({404}),
        )
        await self._request(
            "meeting_b_write_known_a",
            "POST",
            f"/meetings/{self.meeting_id}/inject_segment",
            token=identity_b.token,
            expected=frozenset({404}),
            json={"text": "cross-owner probe", "start_ms": 1001, "end_ms": 2000},
        )
        await self._request(
            "meeting_b_delete_known_a",
            "DELETE",
            f"/meetings/{self.meeting_id}/outputs",
            token=identity_b.token,
            expected=frozenset({404}),
            json={"artifact_ids": [], "clear_minutes": True},
        )
        await self._request(
            "meeting_b_finalize_known_a",
            "POST",
            f"/meetings/{self.meeting_id}/finalize",
            token=identity_b.token,
            expected=frozenset({404}),
            data={"title": "cross-owner finalize probe"},
        )
        await self._request(
            "meeting_b_end_known_a",
            "POST",
            f"/meetings/{self.meeting_id}/end",
            token=identity_b.token,
            expected=frozenset({404}),
        )

    async def _artifact_isolation(
        self,
        identity_a: IssuedIdentity,
        identity_b: IssuedIdentity,
    ) -> None:
        own_collection = await self._request(
            "artifact_a_meeting_collection",
            "GET",
            f"/meetings/{self.meeting_id}/artifacts",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        _list(
            _json_response(own_collection, "artifact_a_collection_json", self.sink),
            "artifact_a_collection_shape",
            self.sink,
            status=own_collection.status_code,
        )
        await self._request(
            "artifact_b_known_a_meeting_collection",
            "GET",
            f"/meetings/{self.meeting_id}/artifacts",
            token=identity_b.token,
            expected=frozenset({404}),
        )

        list_a_response = await self._request(
            "artifact_a_list",
            "GET",
            "/artifacts",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        list_b_response = await self._request(
            "artifact_b_list",
            "GET",
            "/artifacts",
            token=identity_b.token,
            expected=frozenset({200}),
        )
        list_a = _list(
            _json_response(list_a_response, "artifact_a_list_json", self.sink),
            "artifact_a_list_shape",
            self.sink,
            status=list_a_response.status_code,
        )
        list_b = _list(
            _json_response(list_b_response, "artifact_b_list_json", self.sink),
            "artifact_b_list_shape",
            self.sink,
            status=list_b_response.status_code,
        )
        ids_a = {
            str(row["artifact_id"])
            for row in list_a
            if isinstance(row, dict) and isinstance(row.get("artifact_id"), str)
        }
        ids_b = {
            str(row["artifact_id"])
            for row in list_b
            if isinstance(row, dict) and isinstance(row.get("artifact_id"), str)
        }
        self._check("artifact_owner_lists_disjoint", ids_a.isdisjoint(ids_b))

    async def _rag_and_workflow_isolation(
        self,
        identity_a: IssuedIdentity,
        identity_b: IssuedIdentity,
    ) -> None:
        content = (
            f"EchoDesk public isolation smoke document {self.sink.smoke_id}.\n"
            "This document contains no user data and is safe to delete.\n"
        ).encode()
        ingest = await self._request(
            "rag_a_ingest",
            "POST",
            "/rag/ingest",
            token=identity_a.token,
            expected=frozenset({200}),
            files={
                "file": (
                    f"isolation-{self.sink.smoke_id}.txt",
                    content,
                    "text/plain",
                )
            },
            data={"title": f"isolation-{self.sink.smoke_id}", "source": "upload"},
        )
        ingest_payload = _object(
            _json_response(ingest, "rag_a_ingest_json", self.sink),
            "rag_a_ingest_shape",
            self.sink,
            status=ingest.status_code,
        )
        doc_id = _string(ingest_payload.get("doc_id"))
        self._check("rag_a_doc_id_present", doc_id is not None, status=ingest.status_code)
        assert doc_id is not None
        self._rag_doc_id = doc_id

        docs_a_response = await self._request(
            "rag_a_list",
            "GET",
            "/rag/docs",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        docs_a_payload = _object(
            _json_response(docs_a_response, "rag_a_list_json", self.sink),
            "rag_a_list_shape",
            self.sink,
            status=docs_a_response.status_code,
        )
        docs_a = docs_a_payload.get("docs")
        self._check(
            "rag_a_can_see_own_doc",
            isinstance(docs_a, list)
            and any(isinstance(row, dict) and row.get("doc_id") == doc_id for row in docs_a),
            status=docs_a_response.status_code,
        )

        docs_b_response = await self._request(
            "rag_b_list",
            "GET",
            "/rag/docs",
            token=identity_b.token,
            expected=frozenset({200}),
        )
        docs_b_payload = _object(
            _json_response(docs_b_response, "rag_b_list_json", self.sink),
            "rag_b_list_shape",
            self.sink,
            status=docs_b_response.status_code,
        )
        docs_b = docs_b_payload.get("docs")
        self._check(
            "rag_b_cannot_see_a_doc",
            isinstance(docs_b, list)
            and all(not isinstance(row, dict) or row.get("doc_id") != doc_id for row in docs_b),
            status=docs_b_response.status_code,
        )

        runs_a_response = await self._request(
            "workflow_a_list",
            "GET",
            "/workflows/runs",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        runs_a = _list(
            _json_response(runs_a_response, "workflow_a_list_json", self.sink),
            "workflow_a_list_shape",
            self.sink,
            status=runs_a_response.status_code,
        )
        candidates = [
            row
            for row in runs_a
            if isinstance(row, dict)
            and row.get("kind") == "rag.ingest"
            and isinstance(row.get("output"), dict)
            and row["output"].get("doc_id") == doc_id
            and isinstance(row.get("run_id"), str)
        ]
        self._check("workflow_a_ingest_run_present", len(candidates) == 1)
        run_id = str(candidates[0]["run_id"])
        encoded_run_id = _path_segment(run_id)

        await self._request(
            "workflow_a_read_known_run",
            "GET",
            f"/workflows/runs/{encoded_run_id}",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        await self._request(
            "workflow_b_read_known_a_run",
            "GET",
            f"/workflows/runs/{encoded_run_id}",
            token=identity_b.token,
            expected=frozenset({404}),
        )
        await self._request(
            "workflow_b_events_known_a_run",
            "GET",
            f"/workflows/runs/{encoded_run_id}/events",
            token=identity_b.token,
            expected=frozenset({404}),
        )
        await self._request(
            "workflow_b_cancel_known_a_run",
            "POST",
            f"/workflows/runs/{encoded_run_id}/cancel",
            token=identity_b.token,
            expected=frozenset({404}),
            json={"reason": "cross-owner smoke"},
        )
        await self._request(
            "workflow_b_retry_known_a_run",
            "POST",
            f"/workflows/runs/{encoded_run_id}/retry",
            token=identity_b.token,
            expected=frozenset({404}),
            json={"reason": "cross-owner smoke"},
        )
        runs_b_response = await self._request(
            "workflow_b_list",
            "GET",
            "/workflows/runs",
            token=identity_b.token,
            expected=frozenset({200}),
        )
        runs_b = _list(
            _json_response(runs_b_response, "workflow_b_list_json", self.sink),
            "workflow_b_list_shape",
            self.sink,
            status=runs_b_response.status_code,
        )
        self._check(
            "workflow_b_list_excludes_a_run",
            all(not isinstance(row, dict) or row.get("run_id") != run_id for row in runs_b),
            status=runs_b_response.status_code,
        )

        b_delete = await self._request(
            "rag_b_delete_known_a_doc",
            "DELETE",
            f"/rag/docs/{_path_segment(doc_id)}",
            token=identity_b.token,
            expected=frozenset({200, 404}),
        )
        docs_a_after_response = await self._request(
            "rag_a_list_after_b_delete",
            "GET",
            "/rag/docs",
            token=identity_a.token,
            expected=frozenset({200}),
        )
        docs_a_after_payload = _object(
            _json_response(docs_a_after_response, "rag_a_after_b_json", self.sink),
            "rag_a_after_b_shape",
            self.sink,
            status=docs_a_after_response.status_code,
        )
        docs_a_after = docs_a_after_payload.get("docs")
        self._check(
            "rag_b_delete_is_owner_scoped_noop",
            isinstance(docs_a_after, list)
            and any(isinstance(row, dict) and row.get("doc_id") == doc_id for row in docs_a_after),
            status=b_delete.status_code,
        )

    async def _host_admin_isolation(self, identity_a: IssuedIdentity) -> None:
        await self._request(
            "host_admin_data_dir_rejects_public_token",
            "GET",
            "/admin/data-dir",
            token=identity_a.token,
            expected=frozenset({403}),
        )
        await self._request(
            "host_workspace_rejects_public_token",
            "GET",
            "/workspace/status",
            token=identity_a.token,
            expected=frozenset({403}),
        )

    async def _cleanup_request(
        self,
        check: str,
        method: str,
        path: str,
        *,
        token: str,
        expected: frozenset[int],
        **kwargs: Any,
    ) -> None:
        try:
            await self._request(
                check,
                method,
                path,
                token=token,
                expected=expected,
                **kwargs,
            )
        except SmokeFailure:
            self._cleanup_failed = True

    async def _cleanup(self) -> None:
        token_a = self._tokens.get("a")
        token_b = self._tokens.get("b")
        if token_a and self._rag_doc_id:
            await self._cleanup_request(
                "cleanup_rag_a",
                "DELETE",
                f"/rag/docs/{_path_segment(self._rag_doc_id)}",
                token=token_a,
                expected=frozenset({200, 404}),
            )
            try:
                response = await self._request(
                    "cleanup_rag_a_verify",
                    "GET",
                    "/rag/docs",
                    token=token_a,
                    expected=frozenset({200}),
                )
                payload = _object(
                    _json_response(response, "cleanup_rag_verify_json", self.sink),
                    "cleanup_rag_verify_shape",
                    self.sink,
                    status=response.status_code,
                )
                docs = payload.get("docs")
                self._check(
                    "cleanup_rag_a_absent",
                    isinstance(docs, list)
                    and all(
                        not isinstance(row, dict) or row.get("doc_id") != self._rag_doc_id
                        for row in docs
                    ),
                    status=response.status_code,
                )
            except SmokeFailure:
                self._cleanup_failed = True
        if token_a and self._meeting_started:
            await self._cleanup_request(
                "cleanup_meeting_a_outputs",
                "DELETE",
                f"/meetings/{self.meeting_id}/outputs",
                token=token_a,
                expected=frozenset({200, 404}),
                json={"artifact_ids": [], "clear_minutes": True},
            )
            await self._cleanup_request(
                "cleanup_meeting_a_end",
                "POST",
                f"/meetings/{self.meeting_id}/end",
                token=token_a,
                expected=frozenset({200, 404, 409}),
            )
        if token_a:
            await self._cleanup_request(
                "cleanup_identity_a_revoke",
                "POST",
                "/session/revoke",
                token=token_a,
                expected=frozenset({200, 401}),
                json={"scope": "family"},
            )
        if token_b:
            await self._cleanup_request(
                "cleanup_identity_b_revoke",
                "POST",
                "/session/revoke",
                token=token_b,
                expected=frozenset({200, 401}),
                json={"scope": "family"},
            )

    async def run(self) -> None:
        failure: SmokeFailure | None = None
        try:
            unauthenticated = await self._request(
                "unauthenticated_meetings_rejected",
                "GET",
                "/meetings",
                expected=frozenset({401}),
            )
            self._check(
                "unauthenticated_response_is_not_redirect",
                unauthenticated.status_code == 401,
                status=unauthenticated.status_code,
            )

            identity_a = await self._enroll("a")
            identity_b = await self._enroll("b")
            self._check(
                "identities_are_independent",
                identity_a.scope != identity_b.scope
                and identity_a.tenant_id != identity_b.tenant_id
                and identity_a.owner_id != identity_b.owner_id
                and identity_a.device_id != identity_b.device_id,
            )

            await self._websocket_isolation(identity_a, identity_b)
            await self._meeting_isolation(identity_a, identity_b)
            await self._artifact_isolation(identity_a, identity_b)
            await self._rag_and_workflow_isolation(identity_a, identity_b)
            await self._host_admin_isolation(identity_a)
        except SmokeFailure as exc:
            failure = exc
        except Exception:
            self.sink.emit("runtime", ok=False, status=0)
            failure = SmokeFailure("runtime", status=0)
        finally:
            try:
                await self._cleanup()
            except Exception:
                self._cleanup_failed = True
                self.sink.emit("cleanup_runtime", ok=False, status=0)
            try:
                await self.client.aclose()
            except Exception:
                self._cleanup_failed = True
                self.sink.emit("http_client_close", ok=False, status=0)

        if failure is not None:
            raise failure
        if self._cleanup_failed:
            raise SmokeFailure("cleanup")
        self.sink.emit("complete", ok=True)


def _self_test() -> bool:
    smoke_id = _new_smoke_id()
    checks = [
        _SMOKE_ID_RE.fullmatch(smoke_id) is not None,
        _normalize_base_url("https://example.com/", allow_insecure_http=False)
        == "https://example.com",
        _normalize_base_url("http://127.0.0.1:8769", allow_insecure_http=False)
        == "http://127.0.0.1:8769",
        _path_segment("a/b?c") == "a%2Fb%3Fc",
    ]
    try:
        _normalize_base_url("http://192.0.2.1:8769", allow_insecure_http=False)
    except ValueError:
        checks.append(True)
    else:
        checks.append(False)
    try:
        _normalize_base_url(
            "https://user:secret@example.com/?token=secret", allow_insecure_http=True
        )
    except ValueError:
        checks.append(True)
    else:
        checks.append(False)

    secret = secrets.token_urlsafe(32)
    material = EnrollmentMaterial.create(smoke_id, "self-test")
    capture = io.StringIO()
    ResultSink(smoke_id, capture).emit("redaction_contract", ok=True, status=200)
    output = capture.getvalue()
    decoded = json.loads(output)
    checks.extend(
        [
            secret not in output,
            material.enrollment_id not in repr(material),
            material.device_credential not in repr(material),
            decoded
            == {
                "check": "redaction_contract",
                "ok": True,
                "smoke_id": smoke_id,
                "status": 200,
            },
        ]
    )
    return all(checks)


def _read_deployment_gate_token(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("deployment gate file must be absolute")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 256
    ):
        raise ValueError("deployment gate file is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("deployment gate file changed during read")
        raw = os.read(descriptor, 257)
    finally:
        os.close(descriptor)
    token = raw.decode("ascii").strip()
    if not _DEPLOYMENT_GATE_TOKEN_RE.fullmatch(token):
        raise ValueError("deployment gate token is malformed")
    return token


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="EchoDesk API origin，例如 https://example.com")
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="每个 HTTP/WS 操作的最大等待秒数（默认 45）",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="仅用于显式批准的非 loopback 暂存环境；公网正式环境应使用 HTTPS",
    )
    parser.add_argument(
        "--deployment-gate-file",
        help="owner-only 0600 gate token file used only by a closed-gate local cutover smoke",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="只验证 URL 防护、随机 id 与输出脱敏，不访问网络",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.self_test:
        ok = _self_test()
        ResultSink("self-test").emit("self_test", ok=ok)
        return 0 if ok else 1

    smoke_id = _new_smoke_id()
    sink = ResultSink(smoke_id)
    if not args.base_url or not 1 <= args.timeout <= 300:
        sink.emit("arguments", ok=False, status=0)
        return 2
    try:
        base_url = _normalize_base_url(
            str(args.base_url),
            allow_insecure_http=bool(args.allow_insecure_http),
        )
        deployment_gate_token = _read_deployment_gate_token(args.deployment_gate_file)
    except (OSError, UnicodeError, ValueError):
        sink.emit("base_url", ok=False, status=0)
        return 2

    sink.emit("start", ok=True)
    try:
        asyncio.run(
            IsolationSmoke(
                base_url=base_url,
                timeout_s=args.timeout,
                sink=sink,
                deployment_gate_token=deployment_gate_token,
            ).run()
        )
    except (KeyboardInterrupt, Exception):
        sink.emit("complete", ok=False)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
