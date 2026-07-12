#!/usr/bin/env python3
"""Fail when a third-party GitHub Action is not pinned to an immutable commit."""

from __future__ import annotations

import json
import re
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_KEY_RE = re.compile(r"""(?:^|[{,]\s*|-\s+)(?:uses|"uses"|'uses')\s*:\s*""")
SHA_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")
VERIFIED_ACTION_PINS = {
    "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373",
    "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/setup-java@0f481fcb613427c0f801b606911222b5b6f3083a",
    "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "android-actions/setup-android@40fd30fb8d7440372e1316f5d1809ec01dcd3699",
    "reactivecircus/android-emulator-runner@a421e43855164a8197daf9d8d40fe71c6996bb0d",
}


def strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'" and index + 1 < len(line) and line[index + 1] == "'":
                index += 2
                continue
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
        index += 1
    return line


def parse_yaml_scalar(raw: str) -> str:
    value = raw.lstrip()
    if not value:
        raise ValueError("uses value is empty")
    if value.startswith('"'):
        parsed, _ = json.JSONDecoder().raw_decode(value)
        if not isinstance(parsed, str):
            raise ValueError("uses value must be a string")
        return parsed
    if value.startswith("'"):
        result: list[str] = []
        index = 1
        while index < len(value):
            if value[index] == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    result.append("'")
                    index += 2
                    continue
                return "".join(result)
            result.append(value[index])
            index += 1
        raise ValueError("unterminated single-quoted uses value")
    match = re.match(r"[^\s,}\]]+", value)
    if match is None:
        raise ValueError("uses value must be a plain or quoted string")
    return match.group(0)


def workflow_paths(root: Path) -> list[Path]:
    return sorted(
        path for path in root.iterdir() if path.is_file() and path.suffix in {".yml", ".yaml"}
    )


def find_uses(text: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = strip_yaml_comment(raw_line).strip()
        for match in USES_KEY_RE.finditer(line):
            targets.append((line_number, parse_yaml_scalar(line[match.end() :])))
    return targets


def validate_workflows(root: Path = WORKFLOW_ROOT) -> list[str]:
    violations: list[str] = []
    for workflow in workflow_paths(root):
        text = workflow.read_text(encoding="utf-8")
        try:
            uses = find_uses(text)
        except (json.JSONDecodeError, ValueError) as exc:
            violations.append(f"{workflow}: invalid uses entry: {exc}")
            continue
        for line, target in uses:
            if target.startswith("./"):
                continue
            if not SHA_RE.fullmatch(target):
                violations.append(f"{workflow}:{line}: unpinned {target}")
            elif target not in VERIFIED_ACTION_PINS:
                violations.append(f"{workflow}:{line}: unverified {target}")
    return violations


def main() -> int:
    violations = validate_workflows()
    if violations:
        print("GitHub Actions must use a reviewed immutable commit allowlist:")
        print("\n".join(violations))
        return 1
    print("All external GitHub Actions use reviewed immutable commit SHAs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
