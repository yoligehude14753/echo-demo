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


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_pins(path: Path, *, seen: set[Path] | None = None) -> dict[str, str]:
    visited = seen or set()
    resolved = path.resolve()
    if resolved in visited:
        return {}
    visited.add(resolved)
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            pins.update(direct_pins((path.parent / line[3:].strip()).resolve(), seen=visited))
            continue
        match = PIN_RE.match(line)
        if match is None:
            raise ValueError(f"{path}: dependency must use an exact == pin: {raw}")
        pins[normalized(match.group(1))] = match.group(2)
    return pins


def validate_lock(source_name: str, lock_name: str) -> list[str]:
    source = ROOT / source_name
    lock = ROOT / lock_name
    errors: list[str] = []
    if not lock.is_file():
        return [f"missing lock: {lock_name}"]
    text = lock.read_text(encoding="utf-8")
    if "--index-url https://pypi.org/simple" not in text:
        errors.append(f"{lock_name}: official PyPI index is required")
    locked: dict[str, str] = {}
    package_blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text)
    for block in package_blocks:
        first = block.splitlines()[0] if block else ""
        match = LOCK_PIN_RE.match(first)
        if match is None:
            continue
        package = normalized(match.group(1))
        locked[package] = match.group(2)
        if "--hash=sha256:" not in block:
            errors.append(f"{lock_name}: {package} has no SHA-256 hash")
    try:
        expected = direct_pins(source)
    except ValueError as exc:
        return [str(exc)]
    for package, version in expected.items():
        if locked.get(package) != version:
            errors.append(
                f"{lock_name}: direct pin {package}=={version} is missing or stale "
                f"(found {locked.get(package)!r})"
            )
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
