"""P3.2 /admin/settings/remote 单测：GET 脱敏 + PATCH 白名单 + 写入合并。

主要场景：
- GET 返回字段顺序与 _REMOTE_FIELDS 一致；sensitive=True 的 value 已脱敏
- GET source 字段正确反映"是否被 user.json 覆盖过"
- PATCH 写入后 ~/.echodesk/config.json 包含正确 key/value
- PATCH 携带未知 key → 422
- PATCH 空 dict 不写文件、restart_required=False
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """让 user_config_dir() 指向 tmp_path，避免污染 ~/.echodesk/。

    同时清掉 repo 根 .env 里 dev 用的 endpoint 覆盖（dev .env 把 LLM_FAST/STT/TTS
    指向 Tailscale IP）—— 否则本地跑会污染"default vs user"的 source 判定。
    """
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    for k in (
        "LLM_FAST_BASE_URL",
        "STT_FIRERED_URL",
        "TTS_QWEN3_URL",
        "TTS_COSYVOICE_URL",
        "LLM_MAIN_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    # Settings 单例可能在 fixture 之前 import；重置一下让测试拿到隔离的 path
    get_settings.cache_clear()  # type: ignore[attr-defined]
    return tmp_path


@pytest.fixture
def client(isolated_user_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """避免 lifespan 真启动 RAG / repo 等长时副作用：直接构造 app 但不进 lifespan。

    把 get_settings 临时换成 `_env_file=None` 版本，绕过 repo 根 .env 加载，
    让测试看到的 default 就是 config.py 里写的 default（hermetic）。
    """

    def _no_env_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr("app.api.admin.get_settings", _no_env_settings)
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.unit
def test_get_remote_settings_returns_masked_keys(
    client: TestClient,
    isolated_user_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 先在 user.json 写一些值（模拟用户已经配置过）
    user_json = isolated_user_dir / "config.json"
    user_json.parent.mkdir(parents=True, exist_ok=True)
    user_json.write_text(
        json.dumps(
            {
                "yunwu_open_key": "sk-abcdef1234567890",
                "llm_main_base_url": "https://custom.example/v1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # Settings 是 lru_cache，清掉让其重新读 user.json
    get_settings.cache_clear()  # type: ignore[attr-defined]

    r = client.get("/admin/settings/remote")
    assert r.status_code == 200, r.text
    body = r.json()

    # config_path 反映 isolated 目录
    assert str(isolated_user_dir) in body["config_path"]

    fields_by_key = {f["key"]: f for f in body["fields"]}

    # 网关模式两项 + 7 个直连字段都在
    assert set(fields_by_key.keys()) == {
        "echo_gateway_url",
        "echo_gateway_token",
        "llm_main_base_url",
        "yunwu_open_key",
        "llm_fast_base_url",
        "stt_firered_url",
        "tts_qwen3_url",
        "tts_qwen3_voice",
        "tavily_api_key",
    }

    # base_url 明文 + source=user
    llm_url = fields_by_key["llm_main_base_url"]
    assert llm_url["value"] == "https://custom.example/v1"
    assert llm_url["sensitive"] is False
    assert llm_url["source"] == "user"

    # key 脱敏：sk-a***7890（首 4 / 末 4）
    yunwu = fields_by_key["yunwu_open_key"]
    assert yunwu["sensitive"] is True
    assert yunwu["value"] == "sk-a***7890"
    assert yunwu["source"] == "user"

    # 未被 user 覆盖的字段 source=default
    stt = fields_by_key["stt_firered_url"]
    assert stt["source"] == "default"
    # default 已泛化为中性占位（开源脱敏：不再硬编码私有基础设施地址）
    assert stt["value"].startswith("http://")


@pytest.mark.unit
def test_patch_remote_settings_merges_to_config_json(
    client: TestClient,
    isolated_user_dir: Path,
) -> None:
    # 先放一个已有 key 验证 merge 不会丢
    user_json = isolated_user_dir / "config.json"
    user_json.parent.mkdir(parents=True, exist_ok=True)
    user_json.write_text(
        json.dumps({"tts_qwen3_voice": "alice"}, ensure_ascii=False),
        encoding="utf-8",
    )

    r = client.patch(
        "/admin/settings/remote",
        json={
            "updates": {
                "llm_main_base_url": "https://new.example/v1",
                "yunwu_open_key": "sk-new-key-xxx",
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["written_keys"]) == ["llm_main_base_url", "yunwu_open_key"]
    assert body["restart_required"] is True
    assert body["skipped_keys"] == []

    # 文件内容：新写入 + 旧 voice 都在
    data = json.loads(user_json.read_text(encoding="utf-8"))
    assert data["llm_main_base_url"] == "https://new.example/v1"
    assert data["yunwu_open_key"] == "sk-new-key-xxx"
    assert data["tts_qwen3_voice"] == "alice"


@pytest.mark.unit
def test_patch_remote_settings_rejects_unknown_keys(
    client: TestClient,
    isolated_user_dir: Path,
) -> None:
    r = client.patch(
        "/admin/settings/remote",
        json={"updates": {"db_path": "/etc/passwd", "yunwu_open_key": "ok"}},
    )
    assert r.status_code == 422, r.text
    assert "db_path" in r.json()["detail"]
    # 不应该有 partial write：config.json 不存在或不含 yunwu_open_key
    user_json = isolated_user_dir / "config.json"
    assert not user_json.exists() or "yunwu_open_key" not in json.loads(
        user_json.read_text(encoding="utf-8")
    )


@pytest.mark.unit
def test_patch_empty_updates_is_noop(
    client: TestClient,
    isolated_user_dir: Path,
) -> None:
    r = client.patch("/admin/settings/remote", json={"updates": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written_keys"] == []
    assert body["restart_required"] is False
    # 没文件被创建
    assert not (isolated_user_dir / "config.json").exists()


@pytest.mark.unit
def test_mask_short_key() -> None:
    """直接测脱敏函数，短 key 走"首末各 1 字符" 分支。"""
    from app.api.admin import _mask_secret

    assert _mask_secret("") == ""
    assert _mask_secret("abc") == "a***c"
    assert _mask_secret("abcd1234") == "a***4"  # 长度 8
    assert _mask_secret("abcd12345") == "abcd***2345"  # 长度 9 → 走长 key 分支
    assert _mask_secret("sk-1234567890abcd") == "sk-1***abcd"


__all__: list[str] = []  # 让 ruff F401 不抱怨


# 显式的小 settings dependency override（不再需要，但留作 sanity 检查）
def _override_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
