"""HTTP API: 产物生成 / 下载。

POST /artifacts/generate — body { artifact_type, brief, extra_instructions? }
  artifact_type ∈ word | docx | xlsx | excel | pptx | ppt | html
                  | markdown | md | mdown | pdf | txt | text （详见 schemas.artifact）
GET  /artifacts/{id}/download — 下载产物文件，filename 形如
  <safe_title>_<artifact_id>.<ext>（来自 build_dir/meta.json）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import LLMError
from app.adapters.skill import SkillError, SkillExecutor
from app.api.deps import get_event_bus
from app.api.deps import get_llm_singleton as get_llm
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.skill import SkillExecutorPort
from app.schemas.artifact import ArtifactRequest, GeneratedArtifact
from app.schemas.events import EchoEvent
from app.schemas.skill_progress import SkillProgress
from app.use_cases.generate_artifact import generate_artifact

_log = logging.getLogger("echodesk.artifacts")

router = APIRouter(tags=["artifacts"])


_skill_singleton: SkillExecutor | None = None


def get_skill(settings: Settings = Depends(get_settings)) -> SkillExecutorPort:
    global _skill_singleton  # noqa: PLW0603
    if _skill_singleton is None:
        _skill_singleton = SkillExecutor(settings)
    return _skill_singleton


def reset_skill_singleton() -> None:
    global _skill_singleton  # noqa: PLW0603
    _skill_singleton = None


@router.post("/artifacts/generate", response_model=GeneratedArtifact)
async def generate(
    body: ArtifactRequest,
    llm: LLMPort = Depends(get_llm),
    runner: SkillExecutorPort = Depends(get_skill),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> GeneratedArtifact:
    """生成产物。artifact_type 走 ArtifactKind 枚举校验（含 ppt/pptx/word/xlsx/excel/html 别名）。

    M_minutes_refactor：可选携带 ``meeting_id`` + ``todo_id``：
    - 生成成功后回写 ``meetings.minutes_json.todos[todo_id].status="done"``
      + ``artifact_id``，并发 ``meeting.todo.completed`` 事件给前端
    - 任何一边为空则跳过回写（普通产物生成路径不受影响）
    """
    if not body.brief.strip():
        raise HTTPException(status_code=400, detail="brief empty")
    await event_bus.publish(
        EchoEvent(
            type="artifact.generating",
            meeting_id=body.meeting_id,
            payload={"artifact_type": body.artifact_type, "brief": body.brief[:200]},
        )
    )
    try:
        artifact = await generate_artifact(
            runner=runner,
            llm=llm,
            artifact_type=body.artifact_type,
            brief=body.brief,
            extra_instructions=body.extra_instructions,
        )
    except SkillError as e:
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                meeting_id=body.meeting_id,
                payload={"artifact_type": body.artifact_type, "error": str(e)[:300]},
            )
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LLMError as e:
        # P2.3：LLM 远程不可达（Yunwu/heyi-bj 断）也算 graceful failure，
        # 否则前端只能看到 500 静默挂。带 reason="remote_llm" 让前端区分这
        # 类失败（可引导查 StatusBar 云 pill）。
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                meeting_id=body.meeting_id,
                payload={
                    "artifact_type": body.artifact_type,
                    "error": f"远程 LLM 不可达：{str(e)[:200]}",
                    "reason": "remote_llm",
                },
            )
        )
        raise HTTPException(status_code=502, detail=str(e)) from e
    # M_minutes_refactor：todo 回写（artifact 已经生成，回写失败只警告日志，
    # 不影响 artifact.ready 事件正常发出，否则用户看不到产物已生成）
    if body.meeting_id and body.todo_id:
        await _attach_artifact_to_todo_safe(
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            artifact_id=artifact.artifact_id,
        )
    payload = artifact.model_dump(mode="json")
    if body.meeting_id:
        payload["meeting_id"] = body.meeting_id
    if body.todo_id:
        payload["todo_id"] = body.todo_id
    await event_bus.publish(
        EchoEvent(
            type="artifact.ready",
            meeting_id=body.meeting_id,
            payload=payload,
        )
    )
    return artifact


@router.post("/artifacts/generate/stream")
async def generate_stream_endpoint(
    body: ArtifactRequest,
    llm: LLMPort = Depends(get_llm),
    runner: SkillExecutorPort = Depends(get_skill),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> StreamingResponse:
    """流式版产物生成。SSE 推送阶段事件（phase / llm_chunk / done / error）。

    用户原话："不管调用什么工具或者 skill，最好也能流式输出一些过程性的内容"。

    阻塞版 ``POST /artifacts/generate`` 让前端只能看见 spinner，看不到 LLM 实
    时进度（HTML one-pager / xlsx 经常 5-15 分钟）。本端点透传 ``SkillExecutor
    .generate_stream`` 的 ``SkillProgress`` 事件流；前端 ``CommandBar`` 按 stage
    增量 patch Echo 气泡，让用户看到「准备 prompt → 调 LLM → 已收到 N 字符 →
    校验 invariants → 执行渲染 → 落盘」全过程。

    SSE event 协议（与 ``backend/app/api/retrieval.py`` 的 ``/rag/ask`` 同风格）：

    - ``event: phase``      data: ``{phase, msg}``                — prompt_build / invariants_check / executor_run / saved
    - ``event: phase``      data: ``{phase, msg}``                — llm_stream_start / llm_stream_done(+ total_chars/latency_ms)
    - ``event: llm_chunk``  data: ``{text, total_chars}``         — LLM 累积每 ~200 chars 推一次
    - ``event: done``       data: ``GeneratedArtifact``           — 含 artifact_id / artifact_type / file_path / title / size_bytes
    - ``event: error``      data: ``{error, stage}``              — 任一阶段失败（生成器仍会自然终止）

    与原阻塞接口的关系：
    - body 完全相同（ArtifactRequest）
    - 成功路径：done 事件的 data ≡ 阻塞接口的 200 JSON 响应
    - 失败路径：error 事件代替 4xx/5xx；HTTP 状态仍是 200（SSE 已开始就不能改 status）

    todo 回写 / artifact.ready 事件总线 broadcast 与阻塞接口对齐，保证两端行为一致。
    """
    if not body.brief.strip():
        raise HTTPException(status_code=400, detail="brief empty")
    await event_bus.publish(
        EchoEvent(
            type="artifact.generating",
            meeting_id=body.meeting_id,
            payload={"artifact_type": body.artifact_type, "brief": body.brief[:200]},
        )
    )

    return StreamingResponse(
        _stream_skill_progress(
            llm=llm,
            runner=runner,
            event_bus=event_bus,
            body=body,
        ),
        media_type="text/event-stream",
    )


async def _stream_skill_progress(
    *,
    llm: LLMPort,
    runner: SkillExecutorPort,
    event_bus: InMemoryEventBus,
    body: ArtifactRequest,
) -> AsyncIterator[bytes]:
    """消费 ``SkillExecutor.generate_stream``，把每个 ``SkillProgress`` 映射到 SSE event。

    协议设计：
    - 终态事件（``done`` / ``error``）单独 event 类型，前端 onmessage 不用扫 stage
    - 进度事件（除 llm_chunk）统一用 ``event: phase``，data.phase 含 stage 名
    - ``llm_chunk`` 是独立 event，因为前端走另一条更新路径（覆盖 text 而非追加）

    错误处理：``generate_stream`` 抛出 ``SkillError`` / ``LLMError`` 时已经先 yield
    了一条 ``stage="error"``（被本函数翻译成 ``event: error``）；catch 后直接退出
    生成器，不重复发 error。意外异常兜底也发一条 ``event: error``，避免前端永远
    停在 pending。
    """
    last_artifact: GeneratedArtifact | None = None
    last_error: str | None = None
    last_stage: str = "unknown"
    try:
        async for progress in runner.generate_stream(
            llm=llm,
            artifact_type=body.artifact_type,
            brief=body.brief,
            extra_instructions=body.extra_instructions,
        ):
            last_stage = progress.stage
            for frame in _progress_to_sse_frames(progress):
                yield frame
            if progress.stage == "done" and progress.artifact is not None:
                last_artifact = progress.artifact
            elif progress.stage == "error":
                last_error = progress.error or ""
    except (SkillError, LLMError) as e:
        # generate_stream 内部已 yield error 事件再 raise；此处不重复发,仅记录
        # 失败原因供下面的 artifact.failed 事件总线广播。
        last_error = last_error or str(e)
    except Exception as e:  # pragma: no cover - 防御性兜底,避免 SSE 永远 pending
        last_error = last_error or f"unexpected: {e}"
        yield _sse_frame("error", {"error": last_error, "stage": last_stage})

    await _broadcast_stream_outcome(
        event_bus=event_bus,
        body=body,
        artifact=last_artifact,
        error=last_error,
    )


async def _broadcast_stream_outcome(
    *,
    event_bus: InMemoryEventBus,
    body: ArtifactRequest,
    artifact: GeneratedArtifact | None,
    error: str | None,
) -> None:
    """SSE 流终态后通过事件总线广播 ``artifact.ready`` / ``artifact.failed``，与阻塞接口对齐。"""
    if artifact is not None:
        if body.meeting_id and body.todo_id:
            await _attach_artifact_to_todo_safe(
                meeting_id=body.meeting_id,
                todo_id=body.todo_id,
                artifact_id=artifact.artifact_id,
            )
        payload = artifact.model_dump(mode="json")
        if body.meeting_id:
            payload["meeting_id"] = body.meeting_id
        if body.todo_id:
            payload["todo_id"] = body.todo_id
        await event_bus.publish(
            EchoEvent(
                type="artifact.ready",
                meeting_id=body.meeting_id,
                payload=payload,
            )
        )
        return
    if error is not None:
        reason = "remote_llm" if "LLMError" in error or "stream" in error else None
        payload_failed: dict[str, object] = {
            "artifact_type": body.artifact_type,
            "error": error[:300],
        }
        if reason is not None:
            payload_failed["reason"] = reason
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                meeting_id=body.meeting_id,
                payload=payload_failed,
            )
        )


def _progress_to_sse_frames(progress: SkillProgress) -> list[bytes]:
    """把单条 ``SkillProgress`` 转成 1+ 个 SSE 帧（bytes）。

    映射规则：
    - ``llm_chunk`` → ``event: llm_chunk``
    - ``done``     → ``event: done`` (data = GeneratedArtifact JSON)
    - ``error``    → ``event: error`` (data = {error, stage})
    - 其它 stage   → ``event: phase`` (data 含 phase + 可选 msg/total_chars/latency_ms/tool)
    """
    if progress.stage == "llm_chunk":
        chunk_data: dict[str, object] = {
            "text": progress.text or "",
            "total_chars": progress.total_chars or 0,
        }
        return [_sse_frame("llm_chunk", chunk_data)]
    if progress.stage == "done":
        if progress.artifact is None:
            return [_sse_frame("error", {"error": "done event missing artifact", "stage": "done"})]
        return [_sse_frame("done", progress.artifact.model_dump(mode="json"))]
    if progress.stage == "error":
        return [
            _sse_frame(
                "error",
                {"error": progress.error or "", "stage": progress.stage},
            )
        ]
    # phase 类（prompt_build / llm_stream_start / llm_stream_done / invariants_check / executor_run / saved）
    data: dict[str, object] = {"phase": progress.stage}
    if progress.msg is not None:
        data["msg"] = progress.msg
    if progress.total_chars is not None:
        data["total_chars"] = progress.total_chars
    if progress.latency_ms is not None:
        data["latency_ms"] = progress.latency_ms
    if progress.tool is not None:
        data["tool"] = progress.tool
    return [_sse_frame("phase", data)]


def _sse_frame(event: str, data: object) -> bytes:
    """构造一条 SSE 帧：``event: <event>\\ndata: <json>\\n\\n``。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def _attach_artifact_to_todo_safe(*, meeting_id: str, todo_id: str, artifact_id: str) -> None:
    """从 meetings.py 拿 pipeline 单例并尝试回写 todo；任何异常只警告日志。

    use_cases / api 层间用 lazy import 避免循环引用（meetings.py 也 import 这里的
    schemas / 反向风险）。回写失败不抛错——artifact 自身已经成功生成，前端能
    在 ArtifactPanel 看到下载链接；只是 todo checkbox 不会自动划掉。
    """
    try:
        from app.api.meetings import _pipeline

        if _pipeline is None:
            _log.warning(
                "todo writeback skipped: meeting pipeline singleton not initialized "
                "(meeting_id=%s todo_id=%s artifact_id=%s)",
                meeting_id,
                todo_id,
                artifact_id,
            )
            return
        ok = await _pipeline.attach_artifact_to_todo(meeting_id, todo_id, artifact_id)
        if not ok:
            _log.warning(
                "todo writeback miss: meeting_id=%s todo_id=%s artifact_id=%s "
                "(meeting / minutes_json / todo not found)",
                meeting_id,
                todo_id,
                artifact_id,
            )
    except Exception as e:  # pragma: no cover - 防御性，不影响主路径
        _log.warning(
            "todo writeback failed: meeting_id=%s todo_id=%s artifact_id=%s err=%s",
            meeting_id,
            todo_id,
            artifact_id,
            e,
        )


# 跨平台不允许的文件名字符 + 控制字符
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
# 文件名总长度上限（含扩展名）；macOS HFS+ 是 255 字节，留余量给 _<id>.<ext>
_MAX_FILENAME_LEN = 120
# meta.json 缺失或 title 被全部清掉时的兜底
_FALLBACK_TITLE = "untitled"


def _safe_title(raw: str) -> str:
    """将任意 title 字符串归一为可作为文件名片段的安全形式。"""
    s = _UNSAFE_FILENAME_CHARS.sub(" ", raw).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")  # 去首尾空格/句点（Windows 上以 . 结尾会被 strip）
    if not s:
        return _FALLBACK_TITLE
    if len(s) > _MAX_FILENAME_LEN:
        s = s[:_MAX_FILENAME_LEN].rstrip(" .…") or _FALLBACK_TITLE
    return s


@router.get("/artifacts/{artifact_id}/download")
async def download(
    artifact_id: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    build_dir = Path(settings.skill_executor_build_dir).expanduser() / artifact_id
    if not build_dir.exists():
        raise HTTPException(status_code=404, detail="artifact not found")
    candidates = list(build_dir.glob("output.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail="output file missing")
    f = candidates[0]

    # 读 meta.json 拼友好文件名；缺失/坏掉 → 回退到 output.<ext>
    meta_path = build_dir / "meta.json"
    download_name = f.name
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_title = str(meta.get("title", "") or "")
            ext = str(meta.get("ext", "") or f.suffix.lstrip("."))
            safe = _safe_title(raw_title)
            download_name = f"{safe}_{artifact_id}.{ext}" if ext else f"{safe}_{artifact_id}"
        except (OSError, json.JSONDecodeError, ValueError):
            download_name = f.name

    return FileResponse(f, filename=download_name)
