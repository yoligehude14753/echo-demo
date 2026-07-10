"""会议 API：开始/喂 chunk/结束。

设计上音频上传走 multipart（会议端实时切片 30s/段），纪要落地后通过
``/meetings/{id}/minutes`` 拉取，前端清单式展示。

P4-M_meeting_history 新增（2026-05-28）：
- ``GET /meetings``                       前端启动期 hydrate 历史会议列表
- ``GET /meetings/{id}/transcript``       拉指定会议的转写段（``/segments`` 别名）
- ``GET /meetings/{id}/minutes``          反序列化 ``meetings.minutes_json``
- ``GET /meetings/{id}/artifacts``        per-meeting 产物（当前空，留扩展点）

artifacts 的产品决策（PR body 详述）：现 schema ``artifacts`` 无 meeting_id 列，
也没有 meeting_artifacts 关联表。前端 ``store.meetings[*].artifacts`` 是基于 WS
事件 ``artifact.ready.meeting_id`` 维护的 best-effort 视图。这个 endpoint 当前
返回空列表，**调用约定**保留以便后续接入数据库 join；前端在 currentMeetingId
被选中时仍以 store 内的 in-memory 列表为准。
"""

from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.stt import make_stt
from app.api.deps import (
    get_artifact_repository,
    get_diarizer_singleton,
    get_event_bus,
    get_llm_singleton,
    get_meeting_state,
    get_repository,
)
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.meeting import MeetingMinutes, MeetingSummary, TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState

router = APIRouter(prefix="/meetings", tags=["meetings"])

_pipeline: MeetingPipeline | None = None
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class ClearMeetingOutputsRequest(BaseModel):
    artifact_ids: list[str] = Field(default_factory=list)
    clear_minutes: bool = True


class ClearMeetingOutputsResponse(BaseModel):
    meeting_id: str
    minutes_cleared: bool
    artifact_ids: list[str]
    artifacts_deleted: int
    missing_artifact_ids: list[str]


def get_meeting_pipeline(
    settings: Settings = Depends(get_settings),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    repository: RepositoryPort = Depends(get_repository),
    diarizer: DiarizerPort = Depends(get_diarizer_singleton),
) -> MeetingPipeline:
    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        _pipeline = MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diarizer,
            rag=BM25Rag(settings),
            llm=llm,
            event_bus=event_bus,
            repository=repository,
        )
    return _pipeline


def get_meeting_pipeline_for_lifespan(
    settings: Settings,
    repository: RepositoryPort,
) -> MeetingPipeline:
    """lifespan 用：不通过 Depends 注入，直接拿单例（无 LLM/STT/RAG 也能 hydrate）。"""
    from app.api.deps import (
        get_diarizer_singleton as _get_diar,
    )
    from app.api.deps import (
        get_event_bus as _get_bus,
    )
    from app.api.deps import (
        get_llm_singleton as _get_llm,
    )

    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        bus = _get_bus()
        llm = _get_llm(settings)
        diar = _get_diar(settings)
        _pipeline = MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diar,
            rag=BM25Rag(settings),
            llm=llm,
            event_bus=bus,
            repository=repository,
        )
    return _pipeline


def reset_meeting_pipeline() -> None:
    """测试用：清掉缓存的单例。"""
    global _pipeline  # noqa: PLW0603
    _pipeline = None


def _split_artifact_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for item in raw.replace("\n", ",").split(","):
        v = item.strip()
        if v and _ARTIFACT_ID_RE.fullmatch(v) and v not in out:
            out.append(v)
    return out


def _artifact_ids_from_minutes(minutes_json: str | None) -> list[str]:
    if not minutes_json:
        return []
    try:
        data = json.loads(minutes_json)
    except json.JSONDecodeError:
        return []
    ids: list[str] = []
    for todo in data.get("todos", []) or []:
        if not isinstance(todo, dict):
            continue
        artifact_id = todo.get("artifact_id")
        if (
            isinstance(artifact_id, str)
            and _ARTIFACT_ID_RE.fullmatch(artifact_id)
            and artifact_id not in ids
        ):
            ids.append(artifact_id)
    return ids


def _artifact_build_dir(settings: Settings, artifact_id: str) -> Path | None:
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None
    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    candidate = (base / artifact_id).resolve()
    if candidate == base or base not in candidate.parents:
        return None
    return candidate


def _artifact_download_info(settings: Settings, artifact_id: str) -> dict[str, object] | None:
    build_dir = _artifact_build_dir(settings, artifact_id)
    if build_dir is None or not build_dir.exists():
        return None
    candidates = sorted(build_dir.glob("output.*"))
    if not candidates:
        return None
    output = candidates[0]
    title = artifact_id
    artifact_type = output.suffix.lstrip(".") or "file"
    size_bytes = output.stat().st_size
    meta_path = build_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = str(meta.get("title") or title)
            artifact_type = str(meta.get("artifact_type") or meta.get("ext") or artifact_type)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "artifact_id": artifact_id,
        "title": title,
        "artifact_type": artifact_type,
        "size_bytes": size_bytes,
        "download_url": f"/artifacts/{quote(artifact_id)}/download",
    }


def _artifact_file_path(settings: Settings, artifact: GeneratedArtifact) -> Path | None:
    path = Path(artifact.file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    allowed_roots = [
        Path(settings.skill_executor_build_dir).expanduser().resolve(),
        Path(settings.storage_dir).expanduser().resolve(),
    ]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        return None
    return resolved


def _artifact_download_info_from_record(
    settings: Settings,
    artifact: GeneratedArtifact,
) -> dict[str, object] | None:
    path = _artifact_file_path(settings, artifact)
    if path is None or not path.exists():
        return None
    return {
        "artifact_id": artifact.artifact_id,
        "title": artifact.title or artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "size_bytes": artifact.size_bytes,
        "download_url": f"/artifacts/{quote(artifact.artifact_id)}/download",
    }


def _delete_artifact_file(settings: Settings, artifact: GeneratedArtifact) -> bool:
    path = _artifact_file_path(settings, artifact)
    if path is None or not path.exists():
        return False
    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    build_dir = (base / artifact.artifact_id).resolve()
    if path == build_dir or build_dir in path.parents:
        shutil.rmtree(build_dir)
    else:
        path.unlink()
    return True


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _safe_download_name(raw: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", raw).strip()
    return name or "echodesk-minutes"


def _minutes_markdown(
    *,
    meeting_id: str,
    title: str,
    data: dict[str, object],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 会议 ID：{meeting_id}",
    ]
    duration = data.get("duration_sec")
    if isinstance(duration, int | float):
        lines.append(f"- 时长：{round(duration)} 秒")
    created_at = data.get("created_at")
    if created_at:
        lines.append(f"- 生成时间：{created_at}")
    summary = str(data.get("summary") or "").strip()
    lines.extend(["", "## 摘要", "", summary or "会议纪要尚未生成或已被删除。", ""])

    raw_sections = data.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "议题")
        lines.extend([f"## {heading}", ""])
        raw_bullets = sec.get("bullets")
        bullets = raw_bullets if isinstance(raw_bullets, list) else []
        for bullet in bullets:
            lines.append(f"- {bullet}")
        lines.append("")

    raw_decisions = data.get("decisions")
    decisions = raw_decisions if isinstance(raw_decisions, list) else []
    if decisions:
        lines.extend(["## 决议", ""])
        for decision in decisions:
            lines.append(f"- {decision}")
        lines.append("")

    raw_todos = data.get("todos")
    todos = raw_todos if isinstance(raw_todos, list) else []
    if todos:
        lines.extend(["## 待办", ""])
        for item in todos:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                assignee = str(item.get("assignee") or "").strip()
                status = "已完成" if item.get("status") == "done" else "待处理"
                suffix = f"（{assignee}）" if assignee else ""
                if text:
                    lines.append(f"- [{status}] {text}{suffix}")
        lines.append("")

    body = "\n".join(lines).strip()
    return f"{body}\n"


def _share_html(
    *,
    meeting_id: str,
    title: str,
    summary: str | None,
    sections: list[dict[str, object]],
    decisions: list[str],
    artifacts: list[dict[str, object]],
    minutes_download_url: str | None,
) -> str:
    safe_title = html.escape(title)
    safe_meeting = html.escape(meeting_id)
    summary_html = (
        f'<p class="summary">{html.escape(summary)}</p>'
        if summary
        else '<p class="empty">会议纪要尚未生成或已被删除。</p>'
    )
    section_html = ""
    for sec in sections:
        heading = html.escape(str(sec.get("heading") or "议题"))
        raw_bullets = sec.get("bullets")
        bullets: list[object] = raw_bullets if isinstance(raw_bullets, list) else []
        bullet_html = "".join(f"<li>{html.escape(str(b))}</li>" for b in bullets)
        section_html += f"<section><h2>{heading}</h2><ul>{bullet_html}</ul></section>"
    decisions_html = ""
    if decisions:
        decisions_html = (
            "<section><h2>决议</h2><ul>"
            + "".join(f"<li>{html.escape(str(d))}</li>" for d in decisions)
            + "</ul></section>"
        )
    artifact_html = ""
    if artifacts:
        rows = []
        for item in artifacts:
            size_bytes = item.get("size_bytes")
            size = int(size_bytes) if isinstance(size_bytes, int | str) else 0
            rows.append(
                '<a class="artifact" href="{url}">'
                "<span><strong>{title}</strong><em>{kind} · {size}</em></span>"
                "<b>下载</b>"
                "</a>".format(
                    url=html.escape(str(item["download_url"])),
                    title=html.escape(str(item["title"])),
                    kind=html.escape(str(item["artifact_type"])),
                    size=html.escape(_format_bytes(size)),
                )
            )
        artifact_html = (
            '<section><h2>会议产物</h2><div class="artifacts">' + "".join(rows) + "</div></section>"
        )
    else:
        artifact_html = '<section><h2>会议产物</h2><p class="empty">暂无可下载产物。</p></section>'
    action_html = (
        '<div class="actions">'
        f'<a class="primary" href="{html.escape(minutes_download_url)}">保存纪要.md</a>'
        '<a href="#artifacts">查看产物</a>'
        "</div>"
        if minutes_download_url
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>{safe_title} · EchoDesk</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f5f2; color: #26211d; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 28px 18px 44px; }}
    header {{ margin-bottom: 24px; }}
    .brand {{ color: #10a37f; font-size: 13px; font-weight: 700; letter-spacing: .02em; }}
    h1 {{ margin: 8px 0 6px; font-size: clamp(24px, 7vw, 38px); line-height: 1.12; }}
    .mid {{ color: #8b8178; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }}
    .summary {{ font-size: 16px; line-height: 1.8; background: white; border: 1px solid #e4ded7; border-radius: 14px; padding: 16px; }}
    section {{ margin-top: 22px; }}
    h2 {{ font-size: 16px; margin: 0 0 10px; }}
    ul {{ margin: 0; padding-left: 20px; line-height: 1.8; }}
    .empty {{ color: #8b8178; background: #fff; border: 1px dashed #ded7cf; border-radius: 12px; padding: 14px; }}
    .artifacts {{ display: grid; gap: 10px; }}
    .artifact {{ display: flex; justify-content: space-between; gap: 14px; align-items: center; padding: 14px; border: 1px solid #e4ded7; border-radius: 14px; background: white; color: inherit; text-decoration: none; }}
    .artifact strong {{ display: block; font-size: 15px; line-height: 1.3; }}
    .artifact em {{ display: block; margin-top: 4px; color: #8b8178; font-size: 12px; font-style: normal; }}
    .artifact b {{ flex: 0 0 auto; color: #0b7f64; font-size: 13px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
    .actions a {{ display: inline-flex; align-items: center; justify-content: center; min-height: 40px; padding: 0 14px; border-radius: 10px; border: 1px solid #d8d0c8; background: white; color: #26211d; text-decoration: none; font-size: 14px; font-weight: 650; }}
    .actions a.primary {{ background: #10a37f; border-color: #10a37f; color: white; }}
    footer {{ margin-top: 32px; color: #9a9188; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand">EchoDesk 会议资料</div>
      <h1>{safe_title}</h1>
      <div class="mid">{safe_meeting}</div>
      {action_html}
    </header>
    {summary_html}
    {section_html}
    {decisions_html}
    <div id="artifacts">{artifact_html}</div>
    <footer>扫码页面仅用于保存会议纪要与下载产物；删除请回到会议室大屏 EchoDesk 操作。</footer>
  </main>
</body>
</html>"""


@router.get("/current")
async def get_current_meeting(
    response: Response,
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> dict[str, object]:
    """全局会议状态机当前状态：idle 或 in_meeting。

    返回中带 ``minutes_status`` 让前端 MinutesView 能区分「会议中 / 生成中 / 失败 / 已生成」。
    in_meeting → minutes_status=null（会议没结束没纪要可言）
    idle      → 返回最新 meeting 的 minutes_status（若用户刚结束一个会议，UI 据此决定显示什么）
    """
    response.headers["Cache-Control"] = "no-store"
    cur = state.current
    if cur is not None:
        return {
            "mode": "in_meeting",
            "meeting_id": cur.meeting_id,
            "started_at": cur.started_at.isoformat(),
            "started_by": cur.started_by,
            "minutes_status": None,
            "minutes_error": None,
        }
    # idle：探一下最近一条 meeting，把它的 minutes_status 透传出来
    latest = await repository.list_meetings(limit=1)
    latest_rec = latest[0] if latest else None
    return {
        "mode": "idle",
        "meeting_id": None,
        "started_at": None,
        "started_by": None,
        "minutes_status": latest_rec.minutes_status if latest_rec else None,
        "minutes_error": latest_rec.minutes_error if latest_rec else None,
    }


@router.post("/manual_start")
async def manual_start_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    title: str | None = Form(None),
) -> dict[str, object]:
    """用户点击状态栏：手动开始会议。已在会议中则原样返回。"""
    cur = await state.manual_start(title=title)
    return {
        "mode": "in_meeting",
        "meeting_id": cur.meeting_id,
        "started_at": cur.started_at.isoformat(),
        "started_by": cur.started_by,
    }


@router.post("/manual_end")
async def manual_end_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
) -> dict[str, object]:
    """用户点击状态栏：手动结束会议（含 finalize 纪要）。"""
    ended = await state.manual_end()
    return {"mode": "idle", "meeting_id": ended}


@router.get("", response_model=list[MeetingSummary])
async def list_meetings(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    limit: int = 50,
) -> list[MeetingSummary]:
    """会议列表（左侧面板用）。

    按 started_at DESC 倒序，每条带 segments / speakers 计数 + minutes 是否就绪，
    避免前端再发 N 次 detail 请求。

    这是前端启动期 hydrate 的核心入口：早于任何 ws 事件，让用户能马上看到历史
    会议；ws 事件随后只负责维护 in-progress 会议的实时增量。
    """
    rows = await repository.list_meetings(limit=limit)
    out: list[MeetingSummary] = []
    for r in rows:
        n_seg = await repository.count_meeting_segments(r.id)
        n_spk = await repository.count_meeting_speakers(r.id)
        out.append(
            MeetingSummary(
                meeting_id=r.id,
                title=r.title,
                display_title=r.display_title,  # M_minutes_refactor：语义化标题
                state=r.state,
                started_at=r.started_at,
                ended_at=r.ended_at,
                finalized_at=r.finalized_at,
                n_segments=n_seg,
                n_speakers=n_spk,
                has_minutes=bool(r.minutes_json),
            )
        )
    return out


@router.get("/{meeting_id}/transcript", response_model=list[TranscriptSegment])
async def get_transcript(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[TranscriptSegment]:
    """单会议转写流（中间面板用）。

    与 ``/segments`` 等价但语义更显式 + 直接走 repository（不依赖 pipeline 内
    存状态，可拉历史会议）。404 当会议不存在；空列表表示没有 segment 但会议
    本身存在（合法的"刚 start 还没说话"状态）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await repository.list_meeting_segments(meeting_id)


@router.get("/{meeting_id}/minutes", response_model=MeetingMinutes)
async def get_minutes(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> MeetingMinutes:
    """单会议纪要（右上面板用）。

    从 ``meetings.minutes_json`` 反序列化；finalize 之前会议没纪要时返回 404。
    JSON 解析失败抛 502（落库的纪要损坏属于运维问题，不应该让前端默默无展示）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not meeting.minutes_json:
        raise HTTPException(status_code=404, detail="minutes not generated yet")
    try:
        data = json.loads(meeting.minutes_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"minutes_json corrupted: {e!s}") from e
    # 早期落库的 minutes 可能没带 meeting_id；补上保持 schema 完整
    data.setdefault("meeting_id", meeting_id)
    return MeetingMinutes(**data)


@router.get("/{meeting_id}/minutes.md")
async def download_minutes_markdown(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> Response:
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not meeting.minutes_json:
        raise HTTPException(status_code=404, detail="minutes not generated")
    try:
        data = json.loads(meeting.minutes_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="minutes json corrupted") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="minutes json corrupted")

    title = str(data.get("title") or meeting.display_title or meeting.title or meeting_id)
    markdown = _minutes_markdown(meeting_id=meeting_id, title=title, data=data)
    filename = quote(f"{_safe_download_name(title)}.md")
    return Response(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/{meeting_id}/artifacts", response_model=list[GeneratedArtifact])
async def get_meeting_artifacts(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> list[GeneratedArtifact]:
    """单会议产物（右下 outputs 面板用），以 artifact_links 为事实源。"""
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await artifact_repo.list_meeting_artifacts(meeting_id)


@router.get("/{meeting_id}/share", response_class=HTMLResponse)
async def share_meeting(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
    artifact_ids: str | None = Query(None),
) -> HTMLResponse:
    """手机扫码保存会议资料页。

    产物来源以后端 artifact_links 为准。``artifact_ids`` query 只保留兼容旧链接，
    不再参与新事实源判断。
    """
    _ = artifact_ids
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    title = meeting.display_title or meeting.title or meeting_id
    summary: str | None = None
    sections: list[dict[str, object]] = []
    decisions: list[str] = []
    if meeting.minutes_json:
        try:
            data = json.loads(meeting.minutes_json)
            title = str(data.get("title") or title)
            summary = str(data.get("summary") or "") or None
            raw_sections = data.get("sections") if isinstance(data.get("sections"), list) else []
            sections = [s for s in raw_sections if isinstance(s, dict)]
            raw_decisions = data.get("decisions") if isinstance(data.get("decisions"), list) else []
            decisions = [str(d) for d in raw_decisions]
        except json.JSONDecodeError:
            summary = "会议纪要数据损坏，请回到 EchoDesk 重新生成。"

    artifacts = [
        info
        for artifact in await artifact_repo.list_meeting_artifacts(meeting_id)
        if (info := _artifact_download_info_from_record(settings, artifact)) is not None
    ]
    return HTMLResponse(
        _share_html(
            meeting_id=meeting_id,
            title=title,
            summary=summary,
            sections=sections,
            decisions=decisions,
            artifacts=artifacts,
            minutes_download_url=f"/meetings/{quote(meeting_id)}/minutes.md"
            if meeting.minutes_json
            else None,
        )
    )


@router.delete("/{meeting_id}/outputs", response_model=ClearMeetingOutputsResponse)
async def clear_meeting_outputs(
    meeting_id: str,
    body: ClearMeetingOutputsRequest,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> ClearMeetingOutputsResponse:
    """清理会议纪要与产物文件。

    0.3 起 artifact_links 是唯一事实源。请求体里的 ``artifact_ids`` 保留为
    旧客户端兼容字段，但不会被用于扩大删除范围。
    """
    _ = body.artifact_ids
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    artifacts = await artifact_repo.unlink_meeting(meeting_id)
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    deleted = 0
    missing: list[str] = []
    for artifact in artifacts:
        if await artifact_repo.count_links(artifact.artifact_id) > 0:
            continue
        if _delete_artifact_file(settings, artifact):
            deleted += 1
        else:
            missing.append(artifact.artifact_id)
        await artifact_repo.delete_artifact_metadata(artifact.artifact_id)

    if body.clear_minutes:
        await repository.clear_meeting_outputs(meeting_id, clear_minutes=True)

    return ClearMeetingOutputsResponse(
        meeting_id=meeting_id,
        minutes_cleared=body.clear_minutes,
        artifact_ids=artifact_ids,
        artifacts_deleted=deleted,
        missing_artifact_ids=missing,
    )


@router.post("/{meeting_id}/start")
async def start_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> dict[str, str]:
    """启动会议（low-level；建议走 /meetings/manual_start）。"""
    await pipeline.start_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "started"}


@router.post("/{meeting_id}/chunk", response_model=list[TranscriptSegment])
async def add_chunk(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
) -> list[TranscriptSegment]:
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        return await pipeline.add_audio_chunk(meeting_id, audio_bytes, sample_rate=sample_rate)
    except MeetingPipelineError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{meeting_id}/finalize", response_model=MeetingMinutes)
async def finalize(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    title: str = Form(...),
) -> MeetingMinutes:
    """生成或重试生成会议纪要（幂等）。

    幂等语义（2026-05-28 修）：
    - 第一次调用：用 segments + LLM 生成 minutes，写 ``state="finalized"`` + ``minutes_status="ok"``
    - 重试调用（前次失败 → ``state="ended"`` 且 ``minutes_status="generation_failed"``）：
      pipeline 重新装载 repo segments 并重新跑 LLM；成功覆盖原 minutes_json，
      失败再次写 ``generation_failed`` + 新的 ``minutes_error``。
    - 用户视角：「重试生成纪要」按钮就是再 POST 一次 ``/meetings/{id}/finalize``。
    """
    # 重试场景：pipeline 内存里没有这个 meeting 的 segments（重启 / 进程切换）
    # 显式装载一次，避免 finalize 报「no segments」。
    if not pipeline.get_segments(meeting_id):
        rec = await repository.get_meeting(meeting_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")
        loaded = await pipeline.load_meeting_for_retry(meeting_id)
        if not loaded:
            raise HTTPException(
                status_code=400,
                detail=f"meeting {meeting_id} has no segments to summarize",
            )
    try:
        return await pipeline.finalize_meeting(meeting_id, title=title)
    except MeetingPipelineError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{meeting_id}/end")
async def end_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> dict[str, str]:
    """结束会议叠加层（不生成纪要）；ambient 主链路继续。"""
    await pipeline.end_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "ended"}


@router.get("/{meeting_id}/segments", response_model=list[TranscriptSegment])
async def list_segments(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> list[TranscriptSegment]:
    return pipeline.get_segments(meeting_id)


@router.post("/{meeting_id}/inject_segment", response_model=TranscriptSegment)
async def inject_segment(
    meeting_id: str,
    seg: TranscriptSegment,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> TranscriptSegment:
    """演示/兜底入口：当 STT 服务不可用时直接注入已知转写片段。

    用途：
    - demo 录制：把预先准备的逐字稿喂进 pipeline，避开 STT 依赖
    - 离线回放：从 raw_transcript_ref 文件重放
    """
    return await pipeline.append_segment(meeting_id, seg)
