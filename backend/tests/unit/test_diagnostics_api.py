"""diagnostics.py 单测：endpoint 返回 zip + 内容完整 + 敏感字段脱敏 + log 截断。

P2.6（独立产品 Phase 2）：诊断包是用户报 bug 时唯一的 ground truth；
脱敏漏出去 = API key 公开 = 完蛋。所以脱敏 / schema 完整 / log 截断
三类是回归红线。
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from app.api import diagnostics as diag_mod
from app.api import health as health_mod
from app.api.deps import reset_deps_for_test
from app.config import Settings
from app.main import create_app
from app.schemas.events import EchoEvent
from fastapi.testclient import TestClient

# ─────────────────────── fixtures ───────────────────────


@pytest.fixture(autouse=True)
def _isolate_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个 case 用独立 ~/.echodesk/ 影子目录，避免污染开发机真实路径。

    config_io.user_config_dir() 读 ECHO_USER_DIR，所以只要 set env 就够。
    log 目录在该路径下也跟着隔离。
    """
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    # diagnostics 用 get_event_bus() 单例，单例残留会让事件计数串台
    reset_deps_for_test()
    health_mod._cache.clear()


@pytest.fixture
def settings_with_db(tmp_path: Path) -> Settings:
    """建一个含 meetings 表 + schema_version + 100 行假数据的 sqlite 文件。

    诊断包导 db_schema 时只数行数、不导内容；这个 fixture 同时也是后续
    "100 个 meeting" 体积 sanity check 的 base。
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        );
        INSERT INTO schema_version (version, description) VALUES (1, 'initial');
        INSERT INTO schema_version (version, description) VALUES (2, 'schema_version table');

        CREATE TABLE meetings (
            meeting_id TEXT PRIMARY KEY,
            title TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            state TEXT NOT NULL
        );

        CREATE TABLE speakers (
            speaker_id TEXT PRIMARY KEY,
            display_name TEXT,
            embedding_blob BLOB
        );
        """
    )
    for i in range(100):
        conn.execute(
            "INSERT INTO meetings VALUES (?, ?, ?, ?, ?)",
            (f"m-{i:04d}", f"Test Meeting {i}", "2026-05-28T00:00:00+00:00", None, "ended"),
        )
    conn.commit()
    conn.close()

    return Settings(
        db_path=db_path,
        yunwu_open_key="sk-secret123456789",
        tavily_api_key="tvly-also-secret-key-987",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def client(settings_with_db: Settings) -> TestClient:
    """构造 FastAPI app，并把 diagnostics endpoint 的 settings 替换成 fixture 版本。

    其它 endpoint（不在测试范围内）仍走默认 get_settings()，不影响。
    """
    app = create_app()
    app.dependency_overrides[diag_mod.get_settings] = lambda: settings_with_db
    return TestClient(app)


# ─────────────────────── 单元辅助函数 ───────────────────────


@pytest.mark.unit
class TestMask:
    def test_empty(self) -> None:
        assert diag_mod._mask("") == ""
        assert diag_mod._mask(None) == ""

    def test_short_string(self) -> None:
        # 短到不足 12 不能保留可识别前后缀，直接 ***
        assert diag_mod._mask("short") == "***"
        assert diag_mod._mask("abc") == "***"

    def test_long_string_keeps_head_tail_and_length(self) -> None:
        masked = diag_mod._mask("sk-secret123456789")
        # 隐私 sanity：原 secret 子串 "secret123" 绝不能出现在脱敏结果
        assert "secret123" not in masked
        assert masked.startswith("sk-s")
        # 后缀 6789 在 (len=...) 之前；用 in 检查更准确
        assert "***6789" in masked
        assert "(len=18)" in masked


@pytest.mark.unit
class TestRedactSettings:
    def test_redacts_keys_tokens_secrets_passwords(self) -> None:
        data = {
            "yunwu_open_key": "sk-secret123456789",
            "tavily_api_key": "tvly-very-secret-xx",
            "auth_token": "tok-aaaaaaaaaaaa",
            "db_password": "pw-bbbbbbbbbbbb",
            "innocent_field": "normal-value",
            "nested": {
                "another_key": "should-be-masked-too",
                "value": "kept",
            },
        }
        redacted = diag_mod._redact_settings(data)
        assert "secret123" not in redacted["yunwu_open_key"]
        assert "very-secret" not in redacted["tavily_api_key"]
        assert "***" in redacted["auth_token"]
        assert "***" in redacted["db_password"]
        # 非敏感字段原样保留
        assert redacted["innocent_field"] == "normal-value"
        assert redacted["nested"]["value"] == "kept"
        # 嵌套里的 ..._key 也要脱敏
        assert "should-be-masked" not in redacted["nested"]["another_key"]


# ─────────────────────── endpoint 行为 ───────────────────────


@pytest.mark.unit
def test_export_returns_zip(client: TestClient) -> None:
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    cd = r.headers["content-disposition"]
    assert "echodesk-diag-" in cd
    assert cd.endswith('.zip"') or cd.endswith(".zip")
    # zip 文件本体合法可解
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any(n.endswith("/manifest.json") for n in names)


@pytest.mark.unit
def test_export_contains_manifest(client: TestClient) -> None:
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest_name = next(n for n in zf.namelist() if n.endswith("/manifest.json"))
    manifest = json.loads(zf.read(manifest_name))
    assert "version" in manifest
    assert "exported_at" in manifest
    # ISO-8601 解析得通（确认时区信息没丢）
    datetime.fromisoformat(manifest["exported_at"])
    assert isinstance(manifest["items"], list)
    # 至少这些必出现的项目（events / logs 是条件项，不强制）
    for required in ("system", "backend", "healthz", "db_schema", "probes"):
        assert required in manifest["items"], f"manifest missing {required}"


@pytest.mark.unit
def test_export_redacts_api_keys(client: TestClient) -> None:
    """敏感数据底线 —— 原始 key 内容不能出现在任何 zip entry 里。"""
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))

    # 1) backend.json 显式断言：脱敏标记在、原文 substring 不在
    backend_name = next(n for n in zf.namelist() if n.endswith("/backend.json"))
    backend = json.loads(zf.read(backend_name))
    settings_dump = backend["settings_redacted"]
    assert "yunwu_open_key" in settings_dump
    masked_key = settings_dump["yunwu_open_key"]
    assert "secret123" not in masked_key
    assert "***" in masked_key
    assert "(len=" in masked_key
    # tavily 同样脱敏
    assert "very-secret" not in settings_dump.get("tavily_api_key", "")

    # 2) 全 zip sanity：原始 secret 不应该出现在任何文件
    full_dump = b"".join(zf.read(n) for n in zf.namelist())
    assert b"sk-secret123456789" not in full_dump
    assert b"tvly-also-secret-key-987" not in full_dump


@pytest.mark.unit
def test_export_includes_db_schema(client: TestClient) -> None:
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    schema_name = next(n for n in zf.namelist() if n.endswith("/db_schema.json"))
    schema = json.loads(zf.read(schema_name))
    assert schema["ok"] is True
    tables = {t["name"]: t for t in schema["tables"]}
    assert "meetings" in tables, f"missing meetings: have {list(tables.keys())}"
    assert tables["meetings"]["row_count"] == 100
    assert "started_at" in tables["meetings"]["column_names"]
    # schema_sql 必须是 CREATE TABLE 字面值
    assert tables["meetings"]["schema_sql"].lstrip().upper().startswith("CREATE TABLE")
    # schema_version 元信息也包含
    assert schema["schema_versions"] is not None
    assert any(sv["version"] == 2 for sv in schema["schema_versions"])
    # 隐私：行内容不应出现（meetings 表里的 title 是 "Test Meeting 0"）
    raw = zf.read(schema_name)
    assert b"Test Meeting 0" not in raw


@pytest.mark.unit
def test_export_truncates_huge_log(client: TestClient, tmp_path: Path) -> None:
    """log > 5 MB → zip 里只剩 ≤ 5 MB 尾部 + truncated 标注。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    huge = log_dir / "backend.log"
    line = b"x" * 1023 + b"\n"
    with huge.open("wb") as f:
        for _ in range(10 * 1024):  # 10 MB
            f.write(line)

    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    log_entry = next((n for n in zf.namelist() if n.endswith("/logs/backend.log")), None)
    assert log_entry is not None, f"backend.log missing from zip: {zf.namelist()}"
    content = zf.read(log_entry)
    # 严格小于 5 MB + banner（这里给点余量 5.01 MB 上限即可）
    assert len(content) <= 5 * 1024 * 1024 + 256
    # 有 truncated 标注
    assert b"[truncated, original size" in content[:512]

    # manifest 里 log_files 元信息也得反映 truncation
    manifest_name = next(n for n in zf.namelist() if n.endswith("/manifest.json"))
    manifest = json.loads(zf.read(manifest_name))
    log_meta = next(m for m in manifest["log_files"] if m["name"] == "backend.log")
    assert log_meta["truncated"] is True
    assert log_meta["size_bytes"] == 10 * 1024 * 1024


@pytest.mark.unit
def test_export_includes_recent_events(client: TestClient) -> None:
    """InMemoryEventBus 里有事件时，recent_events.jsonl 应该入 zip 并被记进 manifest。"""
    import asyncio

    from app.api.deps import get_event_bus

    bus = get_event_bus()

    async def _push() -> None:
        for i in range(5):
            await bus.publish(EchoEvent(type="chat.delta", payload={"i": i, "text": f"event-{i}"}))

    asyncio.run(_push())

    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    entry = next((n for n in zf.namelist() if n.endswith("/recent_events.jsonl")), None)
    assert entry is not None, f"recent_events missing: {zf.namelist()}"
    lines = zf.read(entry).decode("utf-8").strip().splitlines()
    assert len(lines) == 5
    parsed = [json.loads(line) for line in lines]
    assert all(p["type"] == "chat.delta" for p in parsed)

    manifest = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("/manifest.json"))))
    assert "recent_events" in manifest["items"]
    assert manifest["events_count"] == 5


@pytest.mark.unit
def test_export_skips_events_when_buffer_empty(client: TestClient) -> None:
    """事件 buffer 空（启动期）时，manifest.items 不含 recent_events。"""
    r = client.get("/admin/diagnostics/export")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("/manifest.json"))))
    assert "recent_events" not in manifest["items"]
    assert manifest["events_count"] == 0
    assert not any(n.endswith("/recent_events.jsonl") for n in zf.namelist())


@pytest.mark.unit
def test_export_includes_probes_cache(client: TestClient) -> None:
    """health._cache 有数据时 probes.json 含 entries；空时含 note 字段。"""
    from app.api.health import ProbeResult

    health_mod._cache["speech_recognition"] = ProbeResult(
        ok=True, latency_ms=12.0, checked_at=1700000000.0
    )
    health_mod._cache["main_model"] = ProbeResult(
        ok=None, reason="no_api_key", checked_at=1700000000.0
    )

    r = client.get("/admin/diagnostics/export")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    probes_name = next(n for n in zf.namelist() if n.endswith("/probes.json"))
    probes = json.loads(zf.read(probes_name))
    assert "entries" in probes
    assert probes["entries"]["speech_recognition"]["ok"] is True
    assert probes["entries"]["main_model"]["ok"] is None


@pytest.mark.unit
def test_export_includes_logs_with_rotated(client: TestClient, tmp_path: Path) -> None:
    """同时存在 backend.log + 几个 backend.log.YYYY-MM-DD 时，都进 zip。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "backend.log").write_text("current\n")
    (log_dir / "backend.log.2026-05-27").write_text("yesterday\n")
    (log_dir / "backend.log.2026-05-26").write_text("day-before\n")

    r = client.get("/admin/diagnostics/export")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    log_entries = [n for n in zf.namelist() if "/logs/" in n]
    assert any(n.endswith("/logs/backend.log") for n in log_entries)
    assert any(n.endswith("/logs/backend.log.2026-05-27") for n in log_entries)
    assert any(n.endswith("/logs/backend.log.2026-05-26") for n in log_entries)


@pytest.mark.unit
def test_export_handles_missing_db_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """db 文件不存在时 endpoint 仍要返回 200 + 完整 zip（只是 db_schema.ok=False）。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    settings = Settings(
        db_path=tmp_path / "nonexistent.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app()
    app.dependency_overrides[diag_mod.get_settings] = lambda: settings
    c = TestClient(app)

    r = c.get("/admin/diagnostics/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    schema = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("/db_schema.json"))))
    assert schema["ok"] is False
    assert "missing" in schema["error"]


@pytest.mark.unit
def test_export_size_with_100_meetings_reasonable(client: TestClient) -> None:
    """100 个 meeting + 空 log → 整个诊断包不应超过 500 KB。

    用于 sanity check 体积上限；超出说明可能引入了"误把 db 数据导出"的回归。
    """
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 200
    size_kb = len(r.content) / 1024
    # 经验值：默认下应远小于 200 KB；给到 500 KB 是宽口径门禁，主要拦防爆案
    assert size_kb < 500, f"diag zip 异常膨胀: {size_kb:.1f} KB"


@pytest.mark.unit
def test_build_zip_bytes_is_self_consistent(settings_with_db: Settings) -> None:
    """直接调 _build_zip_bytes 验证 in-memory 路径与 endpoint 路径同口径。"""
    raw, manifest = diag_mod._build_zip_bytes(settings_with_db)
    assert isinstance(raw, bytes)
    assert len(raw) > 0
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()
    assert any(n.endswith("/manifest.json") for n in names)
    assert any(n.endswith("/db_schema.json") for n in names)
    assert manifest["items"]
