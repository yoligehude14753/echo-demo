"""config_io 单测：user.json 三层加载、alias resolve、损坏/缺失安全降级。

P1.2（独立产品 Phase 1）：保证配置三层优先级的契约不被回归破坏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config_io import (
    load_user_config_json,
    user_config_path,
    write_user_config_json,
)


@pytest.fixture
def isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个测试用独立 ECHO_USER_DIR，互不污染、不影响真实 ~/.echodesk/。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.unit
class TestLoadUserConfigJson:
    def test_missing_returns_empty(self, isolated_user_dir: Path) -> None:
        assert load_user_config_json() == {}

    def test_valid_returns_dict(self, isolated_user_dir: Path) -> None:
        cfg = isolated_user_dir / "config.json"
        cfg.write_text(json.dumps({"port": 9999, "stt_language": "en"}), encoding="utf-8")
        d = load_user_config_json()
        assert d == {"port": 9999, "stt_language": "en"}

    def test_keys_lowercased(self, isolated_user_dir: Path) -> None:
        cfg = isolated_user_dir / "config.json"
        cfg.write_text(json.dumps({"PORT": 9999, "Stt_Language": "en"}), encoding="utf-8")
        d = load_user_config_json()
        assert d == {"port": 9999, "stt_language": "en"}

    def test_broken_json_returns_empty(
        self, isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (isolated_user_dir / "config.json").write_text("{ not json", encoding="utf-8")
        assert load_user_config_json() == {}
        assert any("读取失败" in r.message for r in caplog.records)

    def test_non_object_top_level_returns_empty(
        self, isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (isolated_user_dir / "config.json").write_text("[]", encoding="utf-8")
        assert load_user_config_json() == {}
        assert any("不是 object" in r.message for r in caplog.records)


@pytest.mark.unit
class TestWriteUserConfigJson:
    def test_creates_parent_dir(self, isolated_user_dir: Path) -> None:
        # ECHO_USER_DIR 指向 isolated_user_dir，所以 write 走 isolated_user_dir 直接
        # （此前残留的 deep/cfg 变量仅作 doc，已删除以满足 F841）
        write_user_config_json({"port": 9999})
        assert (isolated_user_dir / "config.json").exists()

    def test_merge_default(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"port": 9999})
        write_user_config_json({"stt_language": "en"})
        d = load_user_config_json()
        assert d == {"port": 9999, "stt_language": "en"}

    def test_merge_false_replaces(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"port": 9999})
        write_user_config_json({"stt_language": "en"}, merge=False)
        d = load_user_config_json()
        assert d == {"stt_language": "en"}

    def test_atomic_no_tmp_left(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"port": 9999})
        # 不应有 .config.*.json.tmp 残留
        tmps = list(isolated_user_dir.glob(".config.*.json.tmp"))
        assert tmps == []

    def test_writes_lowercase_keys(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"PORT": 9999, "Stt_Backend": "firered"})
        raw = json.loads(user_config_path().read_text(encoding="utf-8"))
        assert set(raw.keys()) == {"port", "stt_backend"}


@pytest.mark.unit
class TestJsonConfigSource:
    def test_settings_loads_from_user_json(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"port": 9999})
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.port == 9999

    def test_stale_user_app_version_cannot_override_code_version(
        self, isolated_user_dir: Path
    ) -> None:
        write_user_config_json({"app_version": "0.2.43"})
        from app import __version__
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.app_version == __version__

    def test_env_beats_user_json(
        self, isolated_user_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_user_config_json({"port": 9999})
        monkeypatch.setenv("PORT", "8888")
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.port == 8888

    def test_alias_resolved_in_source(self, isolated_user_dir: Path) -> None:
        # 老 .env 写 tts_cosyvoice_url 应该落到 tts_qwen3_url 字段
        write_user_config_json({"tts_cosyvoice_url": "http://example:9999"})
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.tts_qwen3_url == "http://example:9999"

    def test_model_gateway_token_resolved_in_source(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"heyi_gateway_token": "gw-token"})
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.heyi_gateway_token == "gw-token"

    def test_service_api_key_aliases_resolved_in_source(self, isolated_user_dir: Path) -> None:
        write_user_config_json(
            {
                "stt_api_key": "stt-token",
                "tts_api_key": "tts-token",
                "tts_timeout_s": 45,
            }
        )
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.stt_firered_api_key == "stt-token"
        assert s.tts_qwen3_api_key == "tts-token"
        assert s.tts_qwen3_timeout_s == 45

    def test_unknown_field_ignored_with_warning(
        self, isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_user_config_json({"some_future_field": "x", "port": 7777})
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.port == 7777
        assert any("未知字段" in r.message for r in caplog.records)

    def test_missing_user_json_falls_back_to_default(self, isolated_user_dir: Path) -> None:
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.port == 8769  # P1.1 canonical default

    def test_default_main_model_uses_yunwu_deepseek_v4_flash(
        self, isolated_user_dir: Path
    ) -> None:
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.llm_main_provider == "yunwu"
        assert s.llm_main_model == "deepseek-v4-flash"
        assert s.llm_main_base_url == "https://yunwu.ai/v1"

    def test_echo_lan_full_api_env_alias(
        self, isolated_user_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ECHO_LAN_FULL_API_ENABLED", "true")
        from app.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.lan_full_api_enabled is True
