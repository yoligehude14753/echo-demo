#!/usr/bin/env python3
"""Validate that Python lockfiles are hashed and include every direct exact pin."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = {
    "backend/requirements.txt": "backend/requirements.lock",
    "backend/requirements-dev.txt": "backend/requirements-dev.lock",
    "backend/requirements-lint.txt": "backend/requirements-lint.lock",
    "backend/requirements-typecheck.txt": "backend/requirements-typecheck.lock",
    "backend/requirements-audit.txt": "backend/requirements-audit.lock",
    "backend/packaging/requirements-build.txt": "backend/packaging/requirements-build.lock",
}
PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)")
LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)")
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
CPU_RUNTIME_PACKAGES = ("torch", "torchaudio")
FORBIDDEN_ACCELERATOR_PACKAGES = ("cuda-", "nvidia-", "triton")


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def public_version(version: str) -> str:
    """Return the upstream version without a PEP 440 local build suffix."""

    return version.split("+", 1)[0]


def direct_pins(path: Path, *, seen: set[Path] | None = None) -> dict[str, set[str]]:
    visited = seen or set()
    resolved = path.resolve()
    if resolved in visited:
        return {}
    visited.add(resolved)
    pins: dict[str, set[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            for package, versions in direct_pins(
                (path.parent / line[3:].strip()).resolve(), seen=visited
            ).items():
                pins.setdefault(package, set()).update(versions)
            continue
        match = PIN_RE.match(line)
        if match is None:
            raise ValueError(f"{path}: dependency must use an exact == pin: {raw}")
        pins.setdefault(normalized(match.group(1)), set()).add(match.group(2))
    return pins


def _matches_direct_pin(expected: str, actual: str) -> bool:
    if "+" in expected:
        return actual == expected
    return public_version(actual) == expected


def _validate_cpu_runtime_lock(
    lock_name: str,
    text: str,
    expected: dict[str, set[str]],
    locked: dict[str, set[str]],
) -> list[str]:
    if "torch" not in expected:
        return []

    errors: list[str] = []
    if f"--extra-index-url {PYTORCH_CPU_INDEX}" not in text:
        errors.append(f"{lock_name}: official PyTorch CPU index is required")

    for package in CPU_RUNTIME_PACKAGES:
        direct_versions = expected.get(package, set())
        if len(direct_versions) != 1:
            errors.append(
                f"{lock_name}: {package} must have exactly one direct public pin; "
                f"found {sorted(direct_versions) or 'none'}"
            )
            continue
        base = next(iter(direct_versions))
        darwin = re.search(
            rf"(?m)^{re.escape(package)}=={re.escape(base)} ; sys_platform == 'darwin' \\$",
            text,
        )
        cpu = re.search(
            rf"(?m)^{re.escape(package)}=={re.escape(base)}\+cpu ; "
            r"sys_platform != 'darwin' \\$",
            text,
        )
        if darwin is None or cpu is None:
            errors.append(
                f"{lock_name}: {package} must resolve to {base} on Darwin and "
                f"{base}+cpu off Darwin"
            )

    forbidden = sorted(
        package
        for package in locked
        if package == "triton" or package.startswith(FORBIDDEN_ACCELERATOR_PACKAGES[:2])
    )
    if forbidden:
        errors.append(
            f"{lock_name}: CPU runtime must not lock accelerator packages: "
            f"{', '.join(forbidden)}"
        )
    return errors


def validate_lock(source_name: str, lock_name: str) -> list[str]:
    source = ROOT / source_name
    lock = ROOT / lock_name
    errors: list[str] = []
    if not lock.is_file():
        return [f"missing lock: {lock_name}"]
    text = lock.read_text(encoding="utf-8")
    if "--index-url https://pypi.org/simple" not in text:
        errors.append(f"{lock_name}: official PyPI index is required")
    locked: dict[str, set[str]] = {}
    package_blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text)
    for block in package_blocks:
        first = block.splitlines()[0] if block else ""
        match = LOCK_PIN_RE.match(first)
        if match is None:
            continue
        package = normalized(match.group(1))
        locked.setdefault(package, set()).add(match.group(2))
        if "--hash=sha256:" not in block:
            errors.append(f"{lock_name}: {package} has no SHA-256 hash")
    try:
        expected = direct_pins(source)
    except ValueError as exc:
        return [str(exc)]
    for package, versions in expected.items():
        actual_versions = locked.get(package, set())
        if not actual_versions or any(
            not any(_matches_direct_pin(expected_version, actual) for expected_version in versions)
            for actual in actual_versions
        ):
            errors.append(
                f"{lock_name}: direct pin {package}=={sorted(versions)} is missing or stale "
                f"(found {sorted(actual_versions) or None!r})"
            )
    errors.extend(_validate_cpu_runtime_lock(lock_name, text, expected, locked))
    return errors


def main() -> int:
    errors = [error for pair in LOCKS.items() for error in validate_lock(*pair)]
    if errors:
        print("Python dependency lock validation failed:")
        print("\n".join(errors))
        return 1
    print(f"Validated {len(LOCKS)} hashed Python dependency locks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
