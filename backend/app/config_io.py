"""用户配置文件 IO：~/.echodesk/config.json 的读写 + pydantic-settings 源。

P1.2（独立产品 Phase 1）：让 EchoDesk 的配置不再依赖仓库 .env。

配置优先级（高 → 低）：
  1. 环境变量（CI / dev 临时覆盖）
  2. ~/.echodesk/config.json（用户配置，可由 UI 设置面板写）
  3. <repo>/.env 或 cwd/.env（dev 期兼容，prod 找不到也无所谓）
  4. 代码 default（必须能让 backend 自身跑起来，远程依赖可缺省）

例外：`echodesk_base_url` 是 lifecycle owner 注入的运行时事实，不从
config.json 或 .env 读取；否则旧 endpoint 会在新的 backend 生命周期中复活。

设计取舍：
- JSON 不支持嵌套（flat dict，对齐 pydantic-settings env 行为）
- key 不区分大小写（Settings.model_config.case_sensitive=False）
- alias 兼容（pydantic-settings 的 AliasChoices 对所有 source 都生效，
  所以 user.json 里写 `tts_cosyvoice_url` 也会落到 `tts_qwen3_url` 字段）
- 原子写：先 .tmp 再 rename，避免半成品 json 让下次启动崩
- 缺失 / 损坏：safe-fallback 到 {} + WARN log，绝不让 backend 起不来
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger("echodesk.config_io")


# A backend origin is process-lifecycle state, not user preference. Keeping it
# in user JSON or a repository dotenv file lets a stale endpoint silently
# outlive the owner that selected the current runtime endpoint.
RUNTIME_ONLY_FIELDS = frozenset({"echodesk_base_url"})


def user_config_dir() -> Path:
    """用户数据根目录。可被 ECHO_USER_DIR 覆盖（测试用）。"""
    raw = os.environ.get("ECHO_USER_DIR", "~/.echodesk")
    return Path(raw).expanduser()


def user_config_path() -> Path:
    return user_config_dir() / "config.json"


def load_user_config_json() -> dict[str, Any]:
    """安全读：不存在 → {}；JSON parse 失败 → {} + WARN（不抛）。"""
    path = user_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(
                "user config %s 顶层不是 object（实际 %s），忽略",
                path,
                type(data).__name__,
            )
            return {}
        # pydantic-settings case-insensitive 看 env 名，但读 dict 时 key 是字面比较
        # → 这里把 key 统一小写，避免用户写 "PORT" / "Port" / "port" 混乱
        return {str(k).lower(): v for k, v in data.items()}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("user config %s 读取失败 (%s)，回退 code default", path, e)
        return {}


def write_user_config_json(updates: dict[str, Any], *, merge: bool = True) -> Path:
    """原子写。

    merge=True（默认）：跟现有 config.json 合并，只覆盖 updates 里出现的 key。
    merge=False：整文件替换。

    返回写入的 path。父目录自动创建。
    """
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if merge:
        current = load_user_config_json()
        current.update({str(k).lower(): v for k, v in updates.items()})
        payload = current
    else:
        payload = {str(k).lower(): v for k, v in updates.items()}

    # 原子写：tmp → rename。避免半成品 json 让下次启动 parse fail。
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise
    return path


class JsonConfigSource(PydanticBaseSettingsSource):
    """pydantic-settings 源：从 ~/.echodesk/config.json 加载。

    挂在 env source 之下、dotenv 之上，意味着：
    - 环境变量始终最高（dev / CI 可临时覆盖）
    - user.json 覆盖 .env（prod 装机后无 .env 也能正常工作）
    - .env 仅 dev 期兜底

    Alias 处理：pydantic-settings 2.x 在 source 层就要把 user dict 的 key
    映射到 field name（不会自动用 BaseModel 的 AliasChoices 二次解析）。
    所以这里 __call__ 输出的 dict 必须已经是 field-name keyed。
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        excluded_fields: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(settings_cls)
        raw = load_user_config_json()
        # 把 raw（用户可能写 alias 名）映射到 field name keyed dict
        self._data = self._resolve_aliases(settings_cls, raw)
        for field_name in sorted(excluded_fields):
            if self._data.pop(field_name, None) is not None:
                logger.warning(
                    "user config 中的 %s 被忽略；该运行时值必须由 lifecycle owner 注入",
                    field_name,
                )

    @staticmethod
    def _resolve_aliases(settings_cls: type[BaseSettings], raw: dict[str, Any]) -> dict[str, Any]:
        """raw 里出现 field 自身名 / 任意 AliasChoices 备选名 → 都规整到 field name。"""
        if not raw:
            return {}
        resolved: dict[str, Any] = {}
        for field_name, field in settings_cls.model_fields.items():
            # 候选：field 自身 + AliasChoices 里所有 string choice（全部 lower）
            candidates = [field_name.lower()]
            validation_alias = field.validation_alias
            if validation_alias is not None:
                choices = getattr(validation_alias, "choices", None)
                if choices is not None:
                    for c in choices:
                        if isinstance(c, str):
                            candidates.append(c.lower())
            # 取第一个命中（field 自身名优先级最高，后面 alias 按 AliasChoices 顺序）
            for name in candidates:
                if name in raw:
                    resolved[field_name] = raw[name]
                    break
        # 不在任何 field 候选里的 key（用户写错 / 加了未来字段）忽略 + 一行 WARN
        unknown = set(raw.keys()) - {
            cand
            for field_name, field in settings_cls.model_fields.items()
            for cand in (
                [field_name.lower()]
                + [
                    c.lower()
                    for c in (getattr(field.validation_alias, "choices", None) or [])
                    if isinstance(c, str)
                ]
            )
        }
        if unknown:
            logger.warning(
                "user config 里有 %d 个未知字段被忽略: %s",
                len(unknown),
                sorted(unknown),
            )
        return resolved

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class ExcludingSettingsSource(PydanticBaseSettingsSource):
    """Filter runtime-only fields from a delegated settings source.

    Pydantic's dotenv source can return either field names or validation
    aliases. Filtering case-insensitively prevents an old
    ``ECHODESK_BASE_URL`` in a checkout-local `.env` from becoming an implicit
    backend endpoint.
    """

    def __init__(
        self,
        source: PydanticBaseSettingsSource,
        *,
        excluded_fields: frozenset[str],
    ) -> None:
        super().__init__(source.settings_cls)
        self._source = source
        self._excluded = frozenset(name.lower() for name in excluded_fields)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        if field_name.lower() in self._excluded:
            return None, field_name, False
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._source().items()
            if str(key).lower() not in self._excluded
        }
