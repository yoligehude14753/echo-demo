"""架构 Fitness Function：强制单向依赖。

层级（自底向上）：
    schemas → config → ports → use_cases → api
    adapters 实现 ports，被 main.py / api 装配；
    use_cases 严禁 import adapters；
    业务层严禁裸 import openai / anthropic / sqlalchemy.Base / fastapi。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
APP = BACKEND_ROOT / "app"

# ── 禁止规则 ───────────────────────────────────────────────────────
FORBIDDEN_IN_USE_CASES = {
    "app.adapters",
    "openai",
    "anthropic",
    "fastapi",
    "sqlalchemy",  # 业务不直接 import
}

FORBIDDEN_IN_PORTS = {
    "app.adapters",
    "app.use_cases",
    "app.api",
    "openai",
    "anthropic",
    "fastapi",
    "httpx",
    "sqlalchemy",
}

FORBIDDEN_IN_SCHEMAS = {
    "app.adapters",
    "app.use_cases",
    "app.api",
    "app.ports",  # schemas 是更底层
    "openai",
    "anthropic",
    "fastapi",
    "httpx",
    "sqlalchemy",
}

# adapters 可以 import 任何外部依赖，但禁止 import use_cases / api（反向）
FORBIDDEN_IN_ADAPTERS = {
    "app.use_cases",
    "app.api",
}


def _module_name(file_path: Path) -> str:
    """把 backend/app/foo/bar.py 转成 app.foo.bar。"""
    rel = file_path.relative_to(BACKEND_ROOT)
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


def _imports_of(file_path: Path) -> set[str]:
    """提取一个 py 文件的 top-level import 模块名（含子模块）。"""
    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(file_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _files_in(subpkg: str) -> list[Path]:
    root = APP / subpkg
    if not root.exists():
        return []
    return [p for p in root.rglob("*.py") if p.name != "__init__.py" or p.stat().st_size > 0]


ALLOWED_LEAKS: set[tuple[str, str]] = {
    # audio_gate 是纯 DSP 函数（RMS / VAD 阈值过滤），形式上在 adapters/ 下
    # 但不接任何 IO/SDK，被 use_cases.ambient_capture 直接调用是符合架构意图的。
    # 后续把它移到 app/services/ 后此白名单可删（追踪 issue：m6 模块重组）。
    ("app.use_cases.ambient_capture", "app.adapters.audio_gate"),
}


def _violations(files: list[Path], forbidden: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for f in files:
        for imp in _imports_of(f):
            for f_name in forbidden:
                if imp == f_name or imp.startswith(f"{f_name}."):
                    pair = (_module_name(f), imp)
                    if pair in ALLOWED_LEAKS:
                        continue
                    out.append(pair)
    return out


@pytest.mark.arch
def test_use_cases_layer_is_clean() -> None:
    """use_cases 不得 import adapters / fastapi / 第三方 SDK。"""
    files = _files_in("use_cases")
    bad = _violations(files, FORBIDDEN_IN_USE_CASES)
    assert not bad, f"use_cases 违规 import: {bad}"


@pytest.mark.arch
def test_ports_layer_is_pure() -> None:
    """ports 只能是抽象 + 引用 schemas。"""
    files = _files_in("ports")
    bad = _violations(files, FORBIDDEN_IN_PORTS)
    assert not bad, f"ports 违规 import: {bad}"


@pytest.mark.arch
def test_schemas_layer_is_pure() -> None:
    """schemas 是最底层，只能依赖 pydantic + stdlib。"""
    files = _files_in("schemas")
    bad = _violations(files, FORBIDDEN_IN_SCHEMAS)
    assert not bad, f"schemas 违规 import: {bad}"


@pytest.mark.arch
def test_adapters_no_back_reference() -> None:
    """adapters 不得反向引用 use_cases / api。"""
    files = _files_in("adapters")
    bad = _violations(files, FORBIDDEN_IN_ADAPTERS)
    assert not bad, f"adapters 反向引用违规: {bad}"


@pytest.mark.arch
def test_every_port_has_protocol() -> None:
    """每个 ports/*.py 至少定义一个 Protocol 或 ABC。"""
    files = [p for p in (APP / "ports").glob("*.py") if p.name != "__init__.py"]
    assert files, "ports/ 必须至少有一个 Port 定义"
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert ("Protocol" in src) or ("ABC" in src), f"{f.name} 缺少 Protocol/ABC 定义"
