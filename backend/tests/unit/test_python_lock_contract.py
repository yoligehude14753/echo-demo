from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-python-locks.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("echodesk_python_lock_checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(
    root: Path,
    *,
    cpu_version: str = "2.11.0+cpu",
    include_cpu_index: bool = True,
    accelerator_package: str | None = None,
) -> None:
    (root / "requirements.txt").write_text(
        "torch==2.11.0\ntorchaudio==2.11.0\n",
        encoding="utf-8",
    )
    index = (
        "--extra-index-url https://download.pytorch.org/whl/cpu\n"
        if include_cpu_index
        else ""
    )
    lock = (
        "--index-url https://pypi.org/simple\n"
        f"{index}\n"
        "torch==2.11.0 ; sys_platform == 'darwin' \\\n"
        "    --hash=sha256:1111\n"
        f"torch=={cpu_version} ; sys_platform != 'darwin' \\\n"
        "    --hash=sha256:2222\n"
        "torchaudio==2.11.0 ; sys_platform == 'darwin' \\\n"
        "    --hash=sha256:3333\n"
        "torchaudio==2.11.0+cpu ; sys_platform != 'darwin' \\\n"
        "    --hash=sha256:4444\n"
    )
    if accelerator_package is not None:
        lock += f"{accelerator_package}==1.0 \\\n    --hash=sha256:5555\n"
    (root / "requirements.lock").write_text(lock, encoding="utf-8")


@pytest.mark.unit
def test_lock_contract_accepts_cpu_local_build_of_same_public_version(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_fixture(tmp_path)
    checker.ROOT = tmp_path

    assert checker.validate_lock("requirements.txt", "requirements.lock") == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fixture", "expected_error"),
    [
        ({"cpu_version": "2.12.0+cpu"}, "missing or stale"),
        ({"include_cpu_index": False}, "official PyTorch CPU index"),
        ({"accelerator_package": "nvidia-cublas"}, "must not lock accelerator packages"),
    ],
)
def test_lock_contract_rejects_unsafe_cpu_resolution(
    tmp_path: Path,
    fixture: dict[str, object],
    expected_error: str,
) -> None:
    checker = _load_checker()
    _write_fixture(tmp_path, **fixture)  # type: ignore[arg-type]
    checker.ROOT = tmp_path

    errors = checker.validate_lock("requirements.txt", "requirements.lock")

    assert any(expected_error in error for error in errors), errors
