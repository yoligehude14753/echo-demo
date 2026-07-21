"""config_io 单测：user.json 三层加载、alias resolve、损坏/缺失安全降级。

P1.2（独立产品 Phase 1）：保证配置三层优先级的契约不被回归破坏。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.config import Settings
from app.config_io import (
    load_user_config_json,
    user_config_path,
    write_user_config_json,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_env_example_tracks_current_defaults_and_keeps_admin_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = {
        key: value
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }
    fields = Settings.model_fields

    assert values["DIARIZER_MATCH_THRESHOLD"] == str(fields["diarizer_match_threshold"].default)
    assert "DIARIZER_MIN_AUDIO_BYTES" not in values
    assert values["LLM_FAST_PROVIDER"] == fields["llm_fast_provider"].default
    assert values["LLM_FAST_MODEL"] == fields["llm_fast_model"].default
    assert values["LLM_FAST_BASE_URL"] == fields["llm_fast_base_url"].default
    assert values["STT_FIRERED_URL"] == fields["stt_firered_url"].default
    assert values["ASR_SCHEDULER_ENABLED"] == str(
        fields["asr_scheduler_enabled"].default
    ).lower()
    assert values["WORKSPACE_MAX_FILE_MB"] == str(int(fields["workspace_max_file_mb"].default))
    assert values["ALLOWED_ORIGINS"] == fields["allowed_origins"].default
    assert "WEB_ARBITRATION_MODEL" not in values
    assert "web_arbitration_model" not in fields
    assert values["DEBUG_TOKEN"] == ""

    monkeypatch.delenv("DEBUG_TOKEN", raising=False)
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    loaded = Settings(_env_file=REPO_ROOT / ".env.example")  # type: ignore[call-arg]
    assert loaded.debug_token == ""


@pytest.mark.unit
def test_installer_defaults_match_the_product_firered_scheduler_defaults(
    isolated_user_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次 source/bootstrap 安装写出的 STT 配置必须可直接接入产品默认链路。"""

    monkeypatch.delenv("STT_FIRERED_URL", raising=False)
    monkeypatch.delenv("ASR_SCHEDULER_ENABLED", raising=False)
    script = (REPO_ROOT / "scripts" / "install-backend.sh").read_text(encoding="utf-8")
    match = re.search(
        r"DEFAULT_CONFIG=\$\(cat <<'JSON'\n(?P<payload>.*?)\nJSON\n\)",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    defaults = json.loads(match.group("payload"))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert defaults["stt_firered_url"] == settings.stt_firered_url
    assert defaults["asr_scheduler_enabled"] is settings.asr_scheduler_enabled
    assert tuple(defaults["asr_eligible_providers"]) == settings.asr_eligible_providers


@pytest.mark.unit
def test_default_origins_include_local_renderer_5174(
    isolated_user_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert {
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "https://localhost:5174",
        "https://127.0.0.1:5174",
    }.issubset(set(settings.allowed_origins_list))


@pytest.mark.unit
def test_unknown_speaker_auto_meeting_fallback_is_enabled_by_default(
    isolated_user_dir: Path,
) -> None:
    """Packaged clients without diarization must still create automatic meetings."""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.automeet_unknown_speaker_min_active_seconds == 12.0


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

    def test_explicit_user_and_environment_asr_overrides_keep_priority(
        self, isolated_user_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_user_config_json(
            {
                "stt_firered_url": "http://127.0.0.1:8090",
                "asr_scheduler_enabled": False,
            }
        )
        user_settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert user_settings.stt_firered_url == "http://127.0.0.1:8090"
        assert user_settings.asr_scheduler_enabled is False

        monkeypatch.setenv("STT_FIRERED_URL", "https://override.example.test")
        monkeypatch.setenv("ASR_SCHEDULER_ENABLED", "true")
        env_settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert env_settings.stt_firered_url == "https://override.example.test"
        assert env_settings.asr_scheduler_enabled is True

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

    def test_generic_main_key_loads_without_legacy_yunwu_key(self, isolated_user_dir: Path) -> None:
        write_user_config_json({"llm_main_api_key": "generic-token"})
        from app.config import Settings

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.llm_main_api_key == "generic-token"
        assert settings.yunwu_open_key == ""
        assert settings.resolved_llm_main_api_key == "generic-token"

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

    def test_secret_values_are_excluded_from_settings_repr(self, isolated_user_dir: Path) -> None:
        from app.config import Settings

        secret_values = {
            "llm_main_api_key": "main-secret-value",
            "yunwu_open_key": "legacy-secret-value",
            "llm_local_api_key": "local-secret-value",
            "heyi_gateway_token": "gateway-secret-value",
            "stt_firered_api_key": "stt-secret-value",
            "tts_qwen3_api_key": "tts-secret-value",
            "tavily_api_key": "search-secret-value",
            "debug_token": "admin-secret-value",
        }
        settings = Settings(**secret_values, _env_file=None)  # type: ignore[call-arg]
        rendered = repr(settings)

        assert all(value not in rendered for value in secret_values.values())

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

    def test_official_electron_origin_survives_deployment_override(
        self, isolated_user_dir: Path
    ) -> None:
        from app.config import OFFICIAL_ELECTRON_ORIGIN, Settings

        settings = Settings(
            allowed_origins="https://browser.example.test",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.allowed_origins_list == [
            "https://browser.example.test",
            OFFICIAL_ELECTRON_ORIGIN,
        ]

    def test_workspace_state_defaults_to_isolated_user_dir(
        self,
        isolated_user_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import Settings

        monkeypatch.delenv("WORKSPACE_STATE_FILE", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.workspace_state_file == isolated_user_dir / "workspace_state.json"

    def test_default_main_model_uses_yunwu_deepseek_v4_flash(self, isolated_user_dir: Path) -> None:
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
