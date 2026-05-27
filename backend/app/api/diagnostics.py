"""诊断包导出 endpoint：一键打包 backend 运行状态 + log + 配置 + db schema。

P2.6（独立产品 Phase 2）：用户报 bug 时点一次按钮就能拿到完整诊断包，发给我们
做事后定位。当前完全没有这个能力时 bug 报来只有截图 → 改两行需要来回 5 轮邮件。

Zip 结构（详见任务 P2.6 描述）：
  echodesk-diag-YYYYMMDD-HHMMSS/
  ├── manifest.json        元信息：version / exported_at / 包含项列表
  ├── system.json          mac/python/uptime
  ├── backend.json         backend 版本/port/uptime + 脱敏 settings 副本
  ├── healthz.json         /healthz/full 当前响应
  ├── db_schema.json       各表 schema + 行数（不含数据本体；隐私）
  ├── logs/                backend.log 当前 + 最近 7 天 rotated
  ├── probes.json          远程探针 cache（health._cache）
  └── recent_events.jsonl  最近 200 条 WS 事件（InMemoryEventBus._history）

隐私底线（绝不能漏）：
- API key / token / secret / password 全部走 ``_mask`` 脱敏
- DB 行内容**不导出**，只导 schema + row_count
- recent_events 直接 dump，payload 里如有用户文本会原样进 zip
  （用户报 bug 时主动操作；接受这个 trade-off）

性能上限：
- 单个 log 文件 ≤ 5 MB（截尾 + 标注 truncated）
- 整包没有硬上限，但典型规模 < 20 MB
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app import __version__
from app.api import health as health_mod
from app.api.deps import get_event_bus
from app.config import Settings, get_settings
from app.config_io import user_config_dir

logger = logging.getLogger("echodesk.diagnostics")

router = APIRouter(tags=["admin"])

# 单个 log 在 zip 里的上限。超出尾部截 5 MB（保留最近的，丢早期）。
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
# rotated 文件最多带几份（按天）；超过的更老滚动包不进诊断包。
_LOG_ROTATED_MAX_FILES = 7
# WS 事件 buffer 取多少条；不超过 InMemoryEventBus._replay_cap (200)
_EVENTS_MAX = 200

# 名字含这些子串的字段判定为敏感，输出走 _mask
_SENSITIVE_KEY_RE = re.compile(r"(key|token|secret|password)", re.IGNORECASE)


def _mask(s: Any) -> str:
    """对敏感字符串做"首4*末4"脱敏，保留长度。

    - 空 / None → ""
    - 长度 < 12 → "***"（短到无法稳定标识来源）
    - 否则 → "abcd***wxyz (len=20)"
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    if not s:
        return ""
    if len(s) < 12:
        return "***"
    return f"{s[:4]}***{s[-4:]} (len={len(s)})"


def _redact_settings(data: Any, *, _path: str = "") -> Any:
    """递归走 settings.model_dump() 输出，把敏感字段值替换为脱敏后的字符串。

    判定规则：dict key 名字匹配 ``_SENSITIVE_KEY_RE`` → 值整体脱敏；
    嵌套结构里也按 key 名独立判定（不是按 path 传染）。
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _mask(v) if isinstance(v, str | int | float) else "***"
            else:
                out[k] = _redact_settings(v, _path=f"{_path}.{k}")
        return out
    if isinstance(data, list):
        return [_redact_settings(v, _path=f"{_path}[]") for v in data]
    return data


def _system_info() -> dict[str, Any]:
    """OS / runtime / 进程 uptime。

    uptime 取 health._BOOT_TIME 单调时钟基准；它在模块 import 时就已固定，
    跟 /healthz/full 显示的 backend.uptime_s 同一个值，方便交叉对照。
    """
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": sys.version,
        "uptime_s": round(time.monotonic() - health_mod._BOOT_TIME, 1),
    }


def _backend_info(settings: Settings) -> dict[str, Any]:
    """backend 版本 / 端口 / uptime + 脱敏后的 settings 全量副本。

    settings 全量（脱敏后）放进诊断包是为了在客户机上还原"用户实际跑的配置"
    —— 默认值 ≠ 实际值，回归案例里 80% 都是用户改了某个 .env / config.json
    项却忘了说。
    """
    dumped = settings.model_dump(mode="json")
    return {
        "version": __version__,
        "port": settings.port,
        "uptime_s": round(time.monotonic() - health_mod._BOOT_TIME, 1),
        "settings_redacted": _redact_settings(dumped),
    }


def _healthz_snapshot(settings: Settings) -> dict[str, Any]:
    """/healthz/full 当前响应快照（不发 HTTP，直接拼）。

    复刻 health.healthz_full 的逻辑；不用 TestClient 起 app 避免循环依赖。
    """
    return {
        "backend": {
            "ok": True,
            "version": __version__,
            "port": settings.port,
            "uptime_s": round(time.monotonic() - health_mod._BOOT_TIME, 1),
        },
        "db": health_mod._db_status(settings),
        "remote": {
            name: health_mod._probe_to_dict(probe)
            for name, probe in health_mod._cache.items()
        },
        "mic": {"ok": "unknown"},
    }


def _db_schema_snapshot(settings: Settings) -> dict[str, Any]:
    """对每个用户表 dump 表名 / DDL / 列名 / 行数；不导任何行数据。

    隐私底线：用户会议正文、speaker 名字、文档内容都不能进诊断包。
    用同步 sqlite3（read-only URI）避开 aiosqlite 异步上下文。

    返回结构稳定：即使 db 文件不存在或损坏，也返回带 ``error`` 字段的 dict，
    不抛异常 —— 诊断包导出不应该被 db 状态绊住。
    """
    db_path = Path(settings.db_path).expanduser()
    if not db_path.exists():
        return {"ok": False, "error": "db file missing", "path": str(db_path), "tables": []}

    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        return {"ok": False, "error": f"open failed: {e}", "path": str(db_path), "tables": []}

    try:
        cur = conn.cursor()
        # 排除 sqlite 内部表（sqlite_master / sqlite_sequence / sqlite_stat*）
        cur.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        rows = cur.fetchall()

        tables: list[dict[str, Any]] = []
        for name, schema_sql in rows:
            cols_cur = conn.execute(f"PRAGMA table_info({name})")
            col_names = [r[1] for r in cols_cur.fetchall()]

            try:
                count_cur = conn.execute(f"SELECT COUNT(*) FROM {name}")
                row_count: int | None = int(count_cur.fetchone()[0])
            except sqlite3.Error:
                row_count = None

            tables.append(
                {
                    "name": name,
                    "schema_sql": schema_sql,
                    "column_names": col_names,
                    "row_count": row_count,
                }
            )
        # schema_version 表本身的内容是公开元信息（不含用户数据），导出有助于诊断 migration 状态
        schema_version_rows: list[dict[str, Any]] | None = None
        try:
            sv_cur = conn.execute(
                "SELECT version, applied_at, description FROM schema_version ORDER BY version"
            )
            schema_version_rows = [
                {"version": r[0], "applied_at": r[1], "description": r[2]}
                for r in sv_cur.fetchall()
            ]
        except sqlite3.Error:
            schema_version_rows = None

        return {
            "ok": True,
            "path": str(db_path),
            "size_mb": round(db_path.stat().st_size / (1024 * 1024), 3),
            "tables": tables,
            "schema_versions": schema_version_rows,
        }
    finally:
        conn.close()


def _probes_snapshot() -> dict[str, Any]:
    """直接 dump health._cache（每个远程探针最近一次结果）。"""
    if not health_mod._cache:
        return {"note": "cache not yet populated", "entries": {}}
    return {
        "entries": {
            name: health_mod._probe_to_dict(probe)
            for name, probe in health_mod._cache.items()
        },
    }


def _recent_events_jsonl() -> tuple[str, int] | None:
    """从 InMemoryEventBus._history 取最近 N 条事件，转 JSONL 字符串。

    返回 None 表示"事件 buffer 不可用 / 为空 / 拿不到"——manifest 里
    应该跳过 ``recent_events``。
    """
    try:
        bus = get_event_bus()
    except Exception as e:
        logger.warning("diagnostics: get_event_bus failed: %s", e)
        return None
    history = getattr(bus, "_history", None)
    if not history:
        return None
    tail = list(history)[-_EVENTS_MAX:]
    lines: list[str] = []
    for evt in tail:
        try:
            lines.append(evt.model_dump_json())
        except Exception as e:
            logger.warning("diagnostics: drop event due to dump failure: %s", e)
    if not lines:
        return None
    return "\n".join(lines) + "\n", len(lines)


def _read_log_truncated(path: Path) -> tuple[bytes, dict[str, Any]]:
    """读 log 文件，超过上限时只保留尾部 5 MB 并在前面加 truncated 标注。

    返回 (内容 bytes, meta dict)；meta 含原始 size_bytes 与是否 truncated。
    """
    size = path.stat().st_size
    meta = {"name": path.name, "size_bytes": size, "truncated": False}
    if size <= _LOG_FILE_MAX_BYTES:
        return path.read_bytes(), meta

    with path.open("rb") as f:
        f.seek(-_LOG_FILE_MAX_BYTES, os.SEEK_END)
        tail = f.read()
    banner = (
        f"[truncated, original size {size / (1024 * 1024):.2f} MB; "
        f"showing last {_LOG_FILE_MAX_BYTES / (1024 * 1024):.0f} MB]\n"
    ).encode()
    meta["truncated"] = True
    return banner + tail, meta


def _collect_log_files() -> list[Path]:
    """列出要带进诊断包的 log 文件路径。

    - 必带：``backend.log``（当前活跃）
    - 选带：``backend.log.YYYY-MM-DD`` 滚动备份；按修改时间倒序最多 7 份
    """
    log_dir = user_config_dir() / "logs"
    if not log_dir.exists():
        return []
    active = log_dir / "backend.log"
    out: list[Path] = []
    if active.exists():
        out.append(active)

    rotated = sorted(
        (p for p in log_dir.glob("backend.log.*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out.extend(rotated[:_LOG_ROTATED_MAX_FILES])
    return out


def _write_zip_payload(
    zf: zipfile.ZipFile, root: str, settings: Settings
) -> dict[str, Any]:
    """把所有诊断 entry 写进 ``zf``，返回 manifest 内容。

    两个调用方共享同一份装配代码：``_build_zip``（落 temp file）和
    ``_build_zip_bytes``（in-memory，测试 / 未来流式接口用）。
    """
    items: list[str] = []
    log_meta: list[dict[str, Any]] = []
    events_count = 0

    zf.writestr(f"{root}/system.json", json.dumps(_system_info(), indent=2))
    items.append("system")

    zf.writestr(
        f"{root}/backend.json",
        json.dumps(_backend_info(settings), indent=2, ensure_ascii=False),
    )
    items.append("backend")

    zf.writestr(
        f"{root}/healthz.json",
        json.dumps(_healthz_snapshot(settings), indent=2),
    )
    items.append("healthz")

    zf.writestr(
        f"{root}/db_schema.json",
        json.dumps(_db_schema_snapshot(settings), indent=2),
    )
    items.append("db_schema")

    log_paths = _collect_log_files()
    for p in log_paths:
        try:
            content, meta = _read_log_truncated(p)
        except OSError as e:
            logger.warning("diagnostics: skip log %s: %s", p, e)
            continue
        zf.writestr(f"{root}/logs/{p.name}", content)
        log_meta.append(meta)
    if log_meta:
        items.append("logs")

    zf.writestr(f"{root}/probes.json", json.dumps(_probes_snapshot(), indent=2))
    items.append("probes")

    events = _recent_events_jsonl()
    if events is not None:
        jsonl, events_count = events
        zf.writestr(f"{root}/recent_events.jsonl", jsonl)
        items.append("recent_events")

    manifest: dict[str, Any] = {
        "version": __version__,
        "exported_at": datetime.now(UTC).isoformat(),
        "platform": platform.system().lower(),
        "items": items,
        "log_files": log_meta,
        "events_count": events_count,
        "root": root,
    }
    zf.writestr(f"{root}/manifest.json", json.dumps(manifest, indent=2))
    return manifest


def _build_zip(settings: Settings) -> tuple[Path, dict[str, Any]]:
    """组装诊断 zip 到临时文件；返回 (zip path, manifest)。"""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = f"echodesk-diag-{ts}"

    with tempfile.NamedTemporaryFile(
        prefix="echodesk-diag-", suffix=".zip", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = _write_zip_payload(zf, root, settings)
    return tmp_path, manifest


def _build_zip_bytes(settings: Settings) -> tuple[bytes, dict[str, Any]]:
    """In-memory 版本；测试 / 未来流式 endpoint 复用。"""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = f"echodesk-diag-{ts}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = _write_zip_payload(zf, root, settings)
    return buf.getvalue(), manifest


@router.get("/diagnostics/export", summary="导出诊断包（zip）")
async def export_diagnostics(
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """打包系统/配置/log/db schema/探针/事件，返回单个 zip 文件下载。

    路径：``GET /admin/diagnostics/export``（main.py 里以 ``/admin`` prefix 注册）。

    实现细节：
    - zip 落到 temp file，``BackgroundTask`` 在响应发送完后删除（避免堆积）
    - 单次导出耗时上限取决于 db / log 大小；典型 < 2s
    - 不做鉴权（EchoDesk 是本地桌面 app，仅监听 127.0.0.1）
    """
    tmp_path, manifest = _build_zip(settings)
    size_kb = tmp_path.stat().st_size / 1024
    logger.info(
        "diagnostics export: %.1f KB, items=%s, events=%d, logs=%d",
        size_kb,
        manifest["items"],
        manifest["events_count"],
        len(manifest["log_files"]),
    )

    fname = f"{manifest['root']}.zip"
    return FileResponse(
        path=str(tmp_path),
        media_type="application/zip",
        filename=fname,
        background=BackgroundTask(_safe_unlink, tmp_path),
    )


def _safe_unlink(path: Path) -> None:
    """诊断包 temp 文件清理；删除失败不抛（OS 偶尔会延迟释放句柄）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("diagnostics: failed to remove %s: %s", path, e)


__all__ = ["router"]
