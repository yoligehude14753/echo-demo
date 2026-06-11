"""上游路由与转发。

- chat/completions：按 model 路由到 yunwu（主）或 heyi fast，注入网关自己的真实凭证。
- audio/transcriptions：multipart 转发到 FireRed STT。
- audio/speech：json 转发到 Qwen3 TTS，原样回传音频字节。

所有上游凭证在此注入，客户端 token 在 auth 层已剥离，不会到达上游。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.config import GatewaySettings


class UpstreamError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class UpstreamRouter:
    def __init__(self, settings: GatewaySettings) -> None:
        self._s = settings
        self._yunwu_models = settings.yunwu_model_set()
        self._timeout = httpx.Timeout(
            settings.upstream_timeout_s, connect=settings.upstream_connect_timeout_s
        )

    # ── chat 路由 ──────────────────────────────────────────
    def _resolve_chat_upstream(self, model: str | None) -> tuple[str, str]:
        """返回 (base_url, bearer_key)。model 命中 yunwu 名单 → yunwu，否则 heyi fast。"""
        if model and model in self._yunwu_models:
            return self._s.yunwu_base_url.rstrip("/"), self._s.yunwu_open_key
        return self._s.heyi_fast_base_url.rstrip("/"), self._s.heyi_fast_key

    async def chat_completion(self, body: dict) -> httpx.Response:
        base, key = self._resolve_chat_upstream(body.get("model"))
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
        return resp

    async def embeddings(self, body: dict) -> httpx.Response:
        """/v1/embeddings 始终走 yunwu（embedding provider），注入网关 yunwu key。"""
        base = self._s.yunwu_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            resp = await client.post(
                f"{base}/embeddings",
                json=body,
                headers={"Authorization": f"Bearer {self._s.yunwu_open_key}"},
            )
        return resp

    async def chat_completion_stream(
        self, body: dict
    ) -> AsyncIterator[bytes]:
        """SSE 透传：把上游流式 chunk 原样转给客户端。"""
        base, key = self._resolve_chat_upstream(body.get("model"))
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            ) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "ignore")
                    raise UpstreamError(resp.status_code, text[:500])
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk

    # ── STT 转发 ───────────────────────────────────────────
    async def transcribe(
        self, *, file_bytes: bytes, filename: str, content_type: str, form: dict[str, str]
    ) -> httpx.Response:
        url = f"{self._s.heyi_stt_base_url.rstrip('/')}/v1/audio/transcriptions"
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            resp = await client.post(
                url,
                headers={"Authorization": "Bearer x"},
                data=form,
                files={"file": (filename, file_bytes, content_type or "audio/wav")},
            )
        return resp

    # ── TTS 转发 ───────────────────────────────────────────
    async def speech(self, body: dict) -> httpx.Response:
        url = f"{self._s.heyi_tts_base_url.rstrip('/')}/v1/audio/speech"
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": "Bearer x"}
            )
        return resp
