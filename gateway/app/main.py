"""echo-gateway FastAPI 应用入口。

暴露 OpenAI 兼容三接口（chat/completions、audio/transcriptions、audio/speech），
统一 Bearer token 鉴权 + 限流，上游真实凭证由网关注入。
"""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app import __version__
from app.auth import Authenticator
from app.config import GatewaySettings, get_settings
from app.upstream import UpstreamError, UpstreamRouter

settings = get_settings()
app = FastAPI(title="echo-gateway", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_authn = Authenticator(settings)
_router = UpstreamRouter(settings)


def get_settings_dep() -> GatewaySettings:
    return settings


async def require_token(request: Request) -> str:
    return _authn.authenticate(request)


@app.get("/health")
async def health() -> dict:
    """无鉴权健康检查（仅暴露最小信息，不泄露上游地址/凭证）。"""
    return {
        "status": "ok",
        "service": "echo-gateway",
        "version": __version__,
        "tokens_configured": len(settings.token_set()),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _token: str = Depends(require_token)) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid json body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a json object"})

    if body.get("stream"):
        async def gen():
            try:
                async for chunk in _router.chat_completion_stream(body):
                    yield chunk
            except UpstreamError as e:
                err = json.dumps({"error": {"message": e.detail, "code": e.status_code}})
                yield f"data: {err}\n\n".encode()
                yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    resp = await _router.chat_completion(body)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.post("/v1/embeddings")
async def embeddings(request: Request, _token: str = Depends(require_token)) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid json body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a json object"})
    resp = await _router.embeddings(body)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile,
    _token: str = Depends(require_token),
) -> Response:
    form = await request.form()
    fields = {
        k: str(v)
        for k, v in form.items()
        if k != "file" and not hasattr(v, "filename")
    }
    fields.setdefault("model", "firered-asr-aed")
    fields.setdefault("response_format", "json")
    data = await file.read()
    resp = await _router.transcribe(
        file_bytes=data,
        filename=file.filename or "audio.wav",
        content_type=file.content_type or "audio/wav",
        form=fields,
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.post("/v1/audio/speech")
async def speech(request: Request, _token: str = Depends(require_token)) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid json body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a json object"})
    resp = await _router.speech(body)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "audio/wav"),
    )
