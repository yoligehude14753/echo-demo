"""Pure Anthropic and OpenAI-compatible protocol adapters.

The functions in this module only transform Python values.  They do not make
requests, read configuration, resolve credentials, invoke tools, or open
ports.  A stream is considered valid only when its terminal semantics and all
tool inputs can be proven from the received chunks.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    MODEL_SCHEMA_VERSION,
    CanonicalMessage,
    CanonicalToolDefinition,
    ModelRequestEnvelope,
    ModelToolRequest,
    NormalizedEvent,
    ProtocolAdapterError,
    RequestIdentity,
    event,
    freeze_mapping,
    json_object,
    sanitize_provider_message,
)


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """A provider body paired with the local identity that owns it."""

    envelope: ModelRequestEnvelope

    @property
    def identity(self) -> RequestIdentity:
        return self.envelope.identity

    @property
    def body(self) -> Mapping[str, Any]:
        return self.envelope.body

    def as_dict(self) -> dict[str, Any]:
        return self.envelope.as_dict()


def _request_envelope(identity: RequestIdentity, body: Mapping[str, Any]) -> ModelRequestEnvelope:
    fields = identity.model_dump(by_alias=True)
    return ModelRequestEnvelope(
        identity=identity,
        schema_version=MODEL_SCHEMA_VERSION,
        task_id=fields["taskId"],
        operation_key=fields["operationKey"],
        request_id=fields["requestId"],
        config_revision=fields["configRevision"],
        route_id=fields["routeId"],
        body=freeze_mapping(body),
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field_name} must be an object")
    return value


def _non_empty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field_name} must be non-empty")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field_name} must be a non-negative integer")
    return value


def _content_blocks(content: Any, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(content, str):
        return ({"type": "text", "text": content},)
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray, str)):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field_name} must be text or a block list")

    result: list[Mapping[str, Any]] = []
    for index, raw_block in enumerate(content):
        block = _mapping(raw_block, f"{field_name}[{index}]")
        block_type = _non_empty(block.get("type"), f"{field_name}[{index}].type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "text block requires text")
            result.append({"type": "text", "text": text})
        elif block_type == "tool_use":
            tool_use_id = _non_empty(block.get("id", block.get("toolUseId")), "tool_use.id")
            name = _non_empty(block.get("name"), "tool_use.name")
            tool_input = _mapping(block.get("input"), "tool_use.input")
            result.append(
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": dict(tool_input)}
            )
        elif block_type == "tool_result":
            tool_use_id = _non_empty(
                block.get("tool_use_id", block.get("toolUseId")), "tool_result.tool_use_id"
            )
            result_content = block.get("content", "")
            if not isinstance(result_content, (str, list, tuple)):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tool_result.content is invalid")
            result.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_content,
                    "is_error": bool(block.get("is_error", block.get("isError", False))),
                }
            )
        else:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"unknown content block type: {block_type}")
    return tuple(result)


def _canonical_messages(messages: Sequence[Mapping[str, Any] | CanonicalMessage]) -> tuple[CanonicalMessage, ...]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "messages must be an ordered list")

    result: list[CanonicalMessage] = []
    known_tool_ids: set[str] = set()
    for index, raw_message in enumerate(messages):
        if isinstance(raw_message, CanonicalMessage):
            role = raw_message.role
            blocks = raw_message.content
        else:
            message = _mapping(raw_message, f"messages[{index}]")
            role = _non_empty(message.get("role"), f"messages[{index}].role")
            blocks = _content_blocks(message.get("content"), f"messages[{index}].content")
        if role not in {"user", "assistant", "tool"}:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"unsupported message role: {role}")

        normalized_blocks: list[Mapping[str, Any]] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "tool_use":
                if role != "assistant":
                    raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool_use must be assistant content")
                tool_use_id = _non_empty(block.get("id"), "tool_use.id")
                if tool_use_id in known_tool_ids:
                    raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate toolUseId")
                known_tool_ids.add(tool_use_id)
            elif block_type == "tool_result":
                tool_use_id = _non_empty(block.get("tool_use_id"), "tool_result.tool_use_id")
                if tool_use_id not in known_tool_ids:
                    raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "unknown toolUseId in result")
            normalized_blocks.append(dict(block))
        result.append(CanonicalMessage(role=role, content=tuple(normalized_blocks)))
    return tuple(result)


def _canonical_tools(tools: Sequence[Mapping[str, Any] | CanonicalToolDefinition]) -> tuple[CanonicalToolDefinition, ...]:
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes, bytearray)):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tools must be an ordered list")
    result: list[CanonicalToolDefinition] = []
    names: set[str] = set()
    for index, raw_tool in enumerate(tools):
        if isinstance(raw_tool, CanonicalToolDefinition):
            tool = raw_tool
        else:
            raw = _mapping(raw_tool, f"tools[{index}]")
            schema = raw.get("input_schema", raw.get("parameters"))
            tool = CanonicalToolDefinition(
                name=_non_empty(raw.get("name"), f"tools[{index}].name"),
                description=raw.get("description"),
                input_schema=_mapping(schema, f"tools[{index}].input_schema"),
            )
        if tool.name in names:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "duplicate tool name")
        names.add(tool.name)
        if not isinstance(tool.input_schema, Mapping) or tool.input_schema.get("type") != "object":
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tool schema must be an object schema")
        if tool.description is not None and not isinstance(tool.description, str):
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tool description must be text")
        result.append(tool)
    return tuple(result)


def _system_blocks(system: str | Sequence[Mapping[str, Any] | str] | None) -> list[dict[str, str]]:
    if system is None:
        return []
    values: Sequence[Any] = [system] if isinstance(system, str) else system
    if not isinstance(values, Sequence):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "system must be text or an ordered list")
    blocks: list[dict[str, str]] = []
    for index, value in enumerate(values):
        if isinstance(value, str):
            blocks.append({"type": "text", "text": value})
            continue
        block = _mapping(value, f"system[{index}]")
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "system blocks must be text blocks")
        blocks.append({"type": "text", "text": block["text"]})
    return blocks


def _anthropic_tool_choice(value: Any) -> dict[str, Any] | str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in {"auto", "any", "none"}:
        return value
    choice = _mapping(value, "tool_choice")
    choice_type = _non_empty(choice.get("type"), "tool_choice.type")
    if choice_type != "tool":
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "Anthropic tool_choice.type is invalid")
    return {"type": "tool", "name": _non_empty(choice.get("name"), "tool_choice.name")}


def _openai_tool_choice(value: Any) -> dict[str, Any] | str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in {"auto", "required", "none"}:
        return value
    choice = _mapping(value, "tool_choice")
    name = choice.get("name")
    if name is None and isinstance(choice.get("function"), Mapping):
        name = choice["function"].get("name")
    return {"type": "function", "function": {"name": _non_empty(name, "tool_choice.name")}}


def _anthropic_messages(messages: tuple[CanonicalMessage, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if block["type"] == "text":
                blocks.append({"type": "text", "text": block["text"]})
            elif block["type"] == "tool_use":
                blocks.append({"type": "tool_use", "id": block["id"], "name": block["name"], "input": block["input"]})
            elif block["type"] == "tool_result":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["tool_use_id"],
                        "content": block["content"],
                        "is_error": block["is_error"],
                    }
                )
            else:  # defensive: _canonical_messages already rejects this
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unsupported canonical block")
        output.append({"role": message.role, "content": blocks})
    return output


def _openai_messages(messages: tuple[CanonicalMessage, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        text_parts = [block["text"] for block in message.content if block["type"] == "text"]
        tool_uses = [block for block in message.content if block["type"] == "tool_use"]
        tool_results = [block for block in message.content if block["type"] == "tool_result"]
        if message.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_uses:
                item["tool_calls"] = [
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":")),
                        },
                    }
                    for block in tool_uses
                ]
            output.append(item)
            continue
        if text_parts:
            output.append({"role": "user", "content": "\n".join(text_parts)})
        for block in tool_results:
            content = block["content"]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            output.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": content})
    return output


def _provider_tools(tools: tuple[CanonicalToolDefinition, ...], provider: str) -> list[dict[str, Any]]:
    if provider == "anthropic":
        return [
            {"name": tool.name, "description": tool.description, "input_schema": dict(tool.input_schema)}
            for tool in tools
        ]
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.input_schema),
            },
        }
        for tool in tools
    ]


def build_anthropic_request(
    identity: RequestIdentity,
    *,
    model: str,
    system: str | Sequence[Mapping[str, Any] | str] | None,
    messages: Sequence[Mapping[str, Any] | CanonicalMessage],
    tools: Sequence[Mapping[str, Any] | CanonicalToolDefinition] = (),
    max_output_tokens: int,
    tool_choice: Any = None,
    temperature: float | None = None,
    stop_sequences: Sequence[str] = (),
) -> ProviderRequest:
    body: dict[str, Any] = {
        "model": _non_empty(model, "model"),
        "max_tokens": _non_negative_int(max_output_tokens, "max_output_tokens"),
        "stream": True,
        "messages": _anthropic_messages(_canonical_messages(messages)),
    }
    system_blocks = _system_blocks(system)
    canonical_tools = _canonical_tools(tools)
    if system_blocks:
        body["system"] = system_blocks
    if canonical_tools:
        body["tools"] = _provider_tools(canonical_tools, "anthropic")
    choice = _anthropic_tool_choice(tool_choice)
    if choice is not None:
        body["tool_choice"] = choice
    if temperature is not None:
        body["temperature"] = temperature
    if stop_sequences:
        body["stop_sequences"] = list(stop_sequences)
    return ProviderRequest(envelope=_request_envelope(identity, body))


def build_openai_compatible_request(
    identity: RequestIdentity,
    *,
    model: str,
    system: str | Sequence[Mapping[str, Any] | str] | None,
    messages: Sequence[Mapping[str, Any] | CanonicalMessage],
    tools: Sequence[Mapping[str, Any] | CanonicalToolDefinition] = (),
    max_output_tokens: int,
    tool_choice: Any = None,
    temperature: float | None = None,
    stop_sequences: Sequence[str] = (),
) -> ProviderRequest:
    canonical_tools = _canonical_tools(tools)
    system_blocks = _system_blocks(system)
    openai_messages: list[dict[str, Any]] = []
    if system_blocks:
        openai_messages.append({"role": "system", "content": "\n".join(block["text"] for block in system_blocks)})
    openai_messages.extend(_openai_messages(_canonical_messages(messages)))
    body: dict[str, Any] = {
        "model": _non_empty(model, "model"),
        "max_tokens": _non_negative_int(max_output_tokens, "max_output_tokens"),
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": openai_messages,
    }
    if canonical_tools:
        body["tools"] = _provider_tools(canonical_tools, "openai")
    choice = _openai_tool_choice(tool_choice)
    if choice is not None:
        body["tool_choice"] = choice
    if temperature is not None:
        body["temperature"] = temperature
    if stop_sequences:
        body["stop"] = list(stop_sequences)
    return ProviderRequest(envelope=_request_envelope(identity, body))


def _sse_chunks(chunks: Iterable[Mapping[str, Any] | str]) -> Iterator[Mapping[str, Any] | str]:
    """Decode complete SSE records or JSON chunks without any I/O."""

    for chunk in chunks:
        if isinstance(chunk, Mapping):
            yield chunk
            continue
        if not isinstance(chunk, str):
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "stream chunk must be an object or SSE text")
        text = chunk.strip()
        if not text:
            continue
        if text == "[DONE]":
            yield "[DONE]"
            continue
        if "data:" not in text:
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "SSE data is not JSON") from exc
            yield _mapping(decoded, "SSE data")
            continue
        data_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "SSE record has no data")
        data = "\n".join(data_lines).strip()
        if data == "[DONE]":
            yield "[DONE]"
            continue
        try:
            decoded = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "SSE data is not JSON") from exc
        yield _mapping(decoded, "SSE data")


def _check_identity(chunk: Mapping[str, Any], identity: RequestIdentity) -> None:
    if "schemaVersion" in chunk and chunk["schemaVersion"] != MODEL_SCHEMA_VERSION:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "provider chunk schemaVersion is unsupported")
    candidates: list[Mapping[str, Any]] = [chunk]
    for key in ("identity", "requestIdentity", "echoIdentity"):
        value = chunk.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    aliases = {
        "requestId": ("requestId", "request_id"),
        "taskId": ("taskId", "task_id"),
        "operationKey": ("operationKey", "operation_key"),
        "configRevision": ("configRevision", "config_revision"),
        "routeId": ("routeId", "route_id"),
    }
    expected = identity.model_dump(by_alias=True)
    for candidate in candidates:
        for canonical, names in aliases.items():
            present = next((name for name in names if name in candidate), None)
            if present is not None and candidate[present] != expected[canonical]:
                raise ProtocolAdapterError(
                    "MODEL_REQUEST_IDENTITY_MISMATCH",
                    f"stream {canonical} does not match active request",
                )


def _stop_reason(provider: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "finish reason must be text")
    if provider == "anthropic":
        allowed = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}
        if value not in allowed:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown Anthropic stop reason")
        return value
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "content_filter",
    }
    if value not in mapping:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown OpenAI finish reason")
    return mapping[value]


def _usage(provider: str, raw: Any, *, input_default: int | None = None) -> dict[str, Any]:
    usage = _mapping(raw, "usage")
    if provider == "anthropic":
        input_tokens = usage.get("input_tokens", input_default)
        output_tokens = usage.get("output_tokens")
        cache_read = usage.get("cache_read_input_tokens", 0)
        estimated = input_tokens is None
    else:
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details")
        cache_read = details.get("cached_tokens", 0) if isinstance(details, Mapping) else 0
        cache_read = usage.get("cache_read_tokens", cache_read)
        estimated = False
    if input_tokens is None or output_tokens is None:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "usage lacks input/output token counts")
    return {
        "inputTokens": _non_negative_int(input_tokens, "usage.input_tokens"),
        "outputTokens": _non_negative_int(output_tokens, "usage.output_tokens"),
        "cacheReadTokens": _non_negative_int(cache_read, "usage.cache_read_tokens"),
        "estimated": estimated,
    }


def _provider_error(chunk: Mapping[str, Any]) -> tuple[str, str, bool]:
    raw_error = chunk.get("error")
    if isinstance(raw_error, Mapping):
        message = raw_error.get("message", "provider error")
        status = chunk.get("status", chunk.get("status_code"))
        if status is None:
            status = raw_error.get("status", raw_error.get("status_code"))
        error_type = str(raw_error.get("type", "")).lower()
    else:
        message = raw_error if raw_error is not None else chunk.get("message", "provider error")
        status = chunk.get("status", chunk.get("status_code"))
        error_type = ""
    status_int: int | None = None
    with suppress(TypeError, ValueError):
        status_int = int(status)
    is_timeout = status_int in {408, 504} or error_type in {"timeout", "timed_out", "deadline_exceeded"}
    if status_int == 401:
        return "MODEL_CREDENTIAL_MISSING", "model credential is missing or rejected", False
    if status_int == 403:
        return "MODEL_CREDENTIAL_REVOKED", "model credential was revoked", False
    if is_timeout:
        return "MODEL_TIMEOUT", "model provider timed out", True
    retryable = status_int == 429 or (status_int is not None and status_int >= 500)
    if status is not None and str(status).lower() in {"rate_limit", "overloaded", "timeout"}:
        retryable = True
    # Keep the fallback diagnostic provider-neutral and bounded; raw provider
    # messages are never part of the public error event.
    _ = sanitize_provider_message(message)
    return "MODEL_UPSTREAM_ERROR", "model provider returned an error", retryable


@dataclass(slots=True)
class _ToolState:
    index: int
    tool_use_id: str
    name: str
    argument_fragments: list[str] = field(default_factory=list)
    closed: bool = False


@dataclass(slots=True)
class _StreamState:
    identity: RequestIdentity
    provider: str
    events: list[NormalizedEvent] = field(default_factory=list)
    started: bool = False
    terminal: bool = False
    failed: bool = False
    stop_reason: str | None = None
    tools: dict[int, _ToolState] = field(default_factory=dict)
    tool_ids: set[str] = field(default_factory=set)
    input_tokens: int | None = None
    usage_emitted: bool = False
    done_marker: bool = False

    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        if not self.failed:
            self.events.append(event(self.identity, event_type, payload))

    def fail(self, error: ProtocolAdapterError) -> None:
        if self.failed:
            return
        self.failed = True
        self.terminal = True
        self.events.append(
            event(
                self.identity,
                "error",
                {
                    "code": error.code,
                    "retryable": error.retryable,
                    "message": sanitize_provider_message(error.message),
                },
            )
        )

    def ensure_started(self) -> None:
        if not self.started:
            self.started = True
            self.emit("message_start")

    def close_tool(self, tool: _ToolState) -> None:
        if tool.closed:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate tool stop")
        raw_input = "".join(tool.argument_fragments) or "{}"
        typed_input = json_object(raw_input)
        request = ModelToolRequest(
            identity=self.identity,
            tool_use_id=tool.tool_use_id,
            name=tool.name,
            input=typed_input,
            index=tool.index,
        )
        tool.closed = True
        self.emit(
            "tool_stop",
            {"index": tool.index, "toolUseId": tool.tool_use_id, "tool": request.as_dict(), "input": request.as_dict()["input"]},
        )

    def close_message(self) -> None:
        if self.stop_reason is None:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "stream has no finish reason")
        if any(not tool.closed for tool in self.tools.values()):
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "stream stopped with open tool call")
        self.emit("message_stop", {"stopReason": self.stop_reason})
        self.terminal = True


def _new_tool(state: _StreamState, index: int, tool_use_id: Any, name: Any) -> _ToolState:
    index = _non_negative_int(index, "tool index")
    tool_id = _non_empty(tool_use_id, "toolUseId")
    tool_name = _non_empty(name, "tool name")
    if index in state.tools or tool_id in state.tool_ids:
        raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate parallel tool call")
    tool = _ToolState(index=index, tool_use_id=tool_id, name=tool_name)
    state.tools[index] = tool
    state.tool_ids.add(tool_id)
    state.emit("tool_start", {"index": index, "id": tool_id, "toolUseId": tool_id, "name": tool_name})
    return tool


def _anthropic_chunk(state: _StreamState, chunk: Mapping[str, Any]) -> None:  # noqa: PLR0911,PLR0912,PLR0915
    _check_identity(chunk, state.identity)
    chunk_type = chunk.get("type")
    if chunk_type == "ping":
        return
    if chunk_type == "error":
        code, message, retryable = _provider_error(chunk)
        state.fail(ProtocolAdapterError(code, message, retryable=retryable))
        return
    if chunk_type == "message_start":
        if state.started:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "duplicate message_start")
        message = _mapping(chunk.get("message"), "message_start.message")
        usage = message.get("usage")
        if usage is not None:
            usage_obj = _mapping(usage, "message_start.message.usage")
            if "input_tokens" in usage_obj:
                state.input_tokens = _non_negative_int(usage_obj["input_tokens"], "usage.input_tokens")
        state.started = True
        state.emit("message_start")
        return
    if not state.started:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "stream did not start with message_start")
    if chunk_type == "content_block_start":
        index = _non_negative_int(chunk.get("index"), "content block index")
        content_block = _mapping(chunk.get("content_block"), "content_block_start.content_block")
        block_type = content_block.get("type")
        if block_type == "text":
            if index in state.tools:
                raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "content block index reused")
            state.tools[index] = _ToolState(index=index, tool_use_id=f"text-{index}", name="__text__")
            state.emit("text_block_start", {"index": index})
        elif block_type == "tool_use":
            _new_tool(state, index, content_block.get("id"), content_block.get("name"))
        else:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown Anthropic content block type")
        return
    if chunk_type == "content_block_delta":
        index = _non_negative_int(chunk.get("index"), "content block index")
        block = state.tools.get(index)
        if block is None or block.closed:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "delta references unknown content block")
        delta = _mapping(chunk.get("delta"), "content_block_delta.delta")
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            if not isinstance(text, str):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "text_delta.text must be text")
            state.emit("text_delta", {"text": text})
        elif delta_type == "input_json_delta":
            if block.name == "__text__":
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tool input delta targets text block")
            partial = delta.get("partial_json")
            if not isinstance(partial, str):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "partial_json must be text")
            block.argument_fragments.append(partial)
            state.emit("tool_arguments_delta", {"index": index, "json": partial})
        else:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown Anthropic content delta type")
        return
    if chunk_type == "content_block_stop":
        index = _non_negative_int(chunk.get("index"), "content block index")
        block = state.tools.get(index)
        if block is None or block.closed:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate or unknown content block stop")
        if block.name == "__text__":
            block.closed = True
        else:
            state.close_tool(block)
        return
    if chunk_type == "message_delta":
        delta = _mapping(chunk.get("delta"), "message_delta.delta")
        if "stop_reason" in delta and delta["stop_reason"] is not None:
            state.stop_reason = _stop_reason("anthropic", delta["stop_reason"])
        usage = chunk.get("usage")
        if usage is not None:
            usage_payload = _usage("anthropic", usage, input_default=state.input_tokens)
            state.emit("usage", usage_payload)
            state.usage_emitted = True
        return
    if chunk_type == "message_stop":
        state.close_message()
        return
    raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown Anthropic stream chunk type")


def _openai_tool_delta(state: _StreamState, raw_tool: Mapping[str, Any], fallback_index: int) -> None:
    index = raw_tool.get("index", fallback_index)
    index = _non_negative_int(index, "tool_calls.index")
    function = _mapping(raw_tool.get("function"), "tool_calls.function")
    tool = state.tools.get(index)
    if tool is None:
        tool = _new_tool(state, index, raw_tool.get("id"), function.get("name"))
    else:
        if raw_tool.get("id") is not None and raw_tool["id"] != tool.tool_use_id:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "toolUseId changed within parallel call")
        if function.get("name") is not None and function["name"] != tool.name:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool name changed within parallel call")
        if tool.closed:
            raise ProtocolAdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool delta arrived after tool stop")
    arguments = function.get("arguments")
    if arguments is not None:
        if not isinstance(arguments, str):
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "tool arguments delta must be text")
        tool.argument_fragments.append(arguments)
        state.emit("tool_arguments_delta", {"index": index, "json": arguments})


def _openai_chunk(state: _StreamState, chunk: Mapping[str, Any]) -> None:  # noqa: PLR0912
    _check_identity(chunk, state.identity)
    if "error" in chunk:
        code, message, retryable = _provider_error(chunk)
        state.fail(ProtocolAdapterError(code, message, retryable=retryable))
        return
    if chunk.get("object") not in {None, "chat.completion.chunk"}:
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "unknown OpenAI stream object")
    usage = chunk.get("usage")
    choices = chunk.get("choices")
    if choices is None:
        if usage is not None:
            state.ensure_started()
            state.emit("usage", _usage("openai", usage))
            state.usage_emitted = True
            return
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "OpenAI chunk lacks choices")
    if not isinstance(choices, list):
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "OpenAI choices must be a list")
    if not choices:
        if usage is None:
            raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "empty OpenAI choices without usage")
        state.ensure_started()
        state.emit("usage", _usage("openai", usage))
        state.usage_emitted = True
        return
    state.ensure_started()
    for choice in choices:
        item = _mapping(choice, "choices[]")
        delta = _mapping(item.get("delta"), "choices[].delta")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "delta.content must be text")
            state.emit("text_delta", {"text": content})
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "delta.tool_calls must be a list")
            for fallback_index, raw_tool in enumerate(tool_calls):
                _openai_tool_delta(state, _mapping(raw_tool, "delta.tool_calls[]"), fallback_index)
        finish_reason = item.get("finish_reason")
        if finish_reason is not None:
            state.stop_reason = _stop_reason("openai", finish_reason)
            if state.stop_reason == "tool_use":
                for tool in sorted(state.tools.values(), key=lambda value: value.index):
                    if tool.name != "__text__" and not tool.closed:
                        state.close_tool(tool)
    if usage is not None:
        state.emit("usage", _usage("openai", usage))
        state.usage_emitted = True


def _normalize_stream(
    chunks: Iterable[Mapping[str, Any] | str],
    identity: RequestIdentity,
    provider: str,
) -> tuple[NormalizedEvent, ...]:
    state = _StreamState(identity=identity, provider=provider)
    try:
        for raw_chunk in _sse_chunks(chunks):
            if raw_chunk == "[DONE]":
                if provider != "openai":
                    raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "[DONE] is not Anthropic syntax")
                if state.stop_reason is None:
                    raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "[DONE] arrived before finish reason")
                state.done_marker = True
                continue
            if state.terminal:
                raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", "chunk arrived after terminal event")
            if provider == "anthropic":
                _anthropic_chunk(state, _mapping(raw_chunk, "Anthropic chunk"))
            else:
                _openai_chunk(state, _mapping(raw_chunk, "OpenAI chunk"))
            if state.failed:
                break
        if not state.terminal and not state.failed:
            if provider == "openai" and state.stop_reason is not None:
                state.close_message()
            else:
                raise ProtocolAdapterError("MODEL_STREAM_INCOMPLETE", "stream ended without a terminal event")
    except ProtocolAdapterError as error:
        state.fail(error)
    return tuple(state.events)


def normalize_anthropic_stream(
    chunks: Iterable[Mapping[str, Any] | str], identity: RequestIdentity
) -> tuple[NormalizedEvent, ...]:
    """Replay Anthropic SSE/chunks into the common event contract."""

    return _normalize_stream(chunks, identity, "anthropic")


def normalize_openai_compatible_stream(
    chunks: Iterable[Mapping[str, Any] | str], identity: RequestIdentity
) -> tuple[NormalizedEvent, ...]:
    """Replay OpenAI-compatible SSE/chunks into the common event contract."""

    return _normalize_stream(chunks, identity, "openai")
