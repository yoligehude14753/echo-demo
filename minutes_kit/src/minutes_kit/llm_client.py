"""LLMClient 抽象：依赖反转的核心。

本模块不 import 任何 backend.app.* 代码，也不绑定具体的 LLM SDK。
所有 LLM 调用走 LLMClient Protocol，由调用方注入实现。
这是日后能干净接入 echo 而不互相污染的前提。

内置实现：
  - OpenAICompatClient：用 httpx 直打 OpenAI 兼容端点（无 SDK 依赖），
    支持任何 /v1/chat/completions 接口（OpenAI / vLLM / m27 proxy 等）。
    仅用于 minutes_kit 独立 demo / CLI；真正接入 echo 时应替换为
    走中台 yoli_llm 的 EchoBridgeClient（详见 INTEGRATION.md §Step 2）。

未来接入：EchoBridgeClient（占位，在 INTEGRATION.md 里描述如何写）
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol, runtime_checkable

import httpx
from loguru import logger


@runtime_checkable
class LLMClient(Protocol):
    """门面：实现这两个方法即可。"""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        temperature: float = 0.3,
    ) -> str:
        """返回纯文本补全。"""
        ...

    async def complete_with_schema(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """返回严格 JSON（model 输出会被 json.loads；非法时抛 ValueError）。"""
        ...


class OpenAICompatClient:
    """OpenAI 兼容端点客户端（仅 httpx，无 SDK 依赖）。

    仅供 minutes_kit 独立 demo / CLI 使用，**不要在 echo 主仓业务路径里调用**。
    生产路径请实现 EchoBridgeClient，通过中台 yoli_llm 走统一 LLM 网关。

    环境变量：
      OPENAI_API_KEY     必填（指向真 OpenAI 时用真 key；指向 m27 proxy 可用 "dummy"）
      OPENAI_BASE_URL    可选（默认 https://api.openai.com/v1）
      MINUTES_KIT_MODEL  可选（默认 gpt-4o-mini）

    设计：
      - 走 POST {base_url}/chat/completions
      - complete_with_schema 携带 response_format={"type": "json_schema", ...} 强制结构化
      - 后端不支持 response_format 时自动降级为纯文本 + JSON 容错解析
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or "dummy"
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("MINUTES_KIT_MODEL") or "gpt-4o-mini"
        self._timeout = timeout

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as cli:
            resp = await cli.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        temperature: float = 0.3,
    ) -> str:
        use_model = model or self.model
        logger.debug(f"[OpenAICompatClient.complete] model={use_model} max_tokens={max_tokens}")
        data = await self._post(
            {
                "model": use_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    async def complete_with_schema(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        use_model = model or self.model
        logger.debug(f"[OpenAICompatClient.complete_with_schema] model={use_model}")
        payload = {
            "model": use_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": schema,
        }
        try:
            data = await self._post(payload)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                f"[OpenAICompatClient] response_format 被后端拒绝 ({exc.response.status_code})，"
                "降级为纯文本 + JSON 容错解析"
            )
            payload.pop("response_format", None)
            data = await self._post(payload)

        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_json_lenient(raw)


# 向后兼容别名（旧代码仍可 `from minutes_kit.llm_client import OpenAIClient`）
OpenAIClient = OpenAICompatClient


class EchoBridgeClient:
    """占位：未来 echo backend 接入时实现。本模块内不实例化。

    实现细节见 INTEGRATION.md §Step 2。
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "EchoBridgeClient 是接入用占位，本次 plan 范围内不实现。"
            "详见 minutes_kit/INTEGRATION.md §Step 2"
        )


def _parse_json_lenient(raw: str) -> dict[str, Any]:
    """容错 JSON 解析：处理 ```json``` 围栏 / 前后多余文本。"""
    if not raw:
        raise ValueError("LLM 返回空字符串")
    raw = raw.strip()
    # 剥代码块围栏
    if raw.startswith("```"):
        lines = raw.split("\n")
        if len(lines) > 2:
            # 去掉首行 ```json 和末行 ```
            end = -1 if lines[-1].strip().startswith("```") else len(lines)
            raw = "\n".join(lines[1:end])
            raw = raw.strip()
    # 截取第一个 { 到最后一个 } 之间
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"LLM 返回不含合法 JSON 对象: {raw[:200]!r}")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}; raw={raw[:200]!r}") from exc
