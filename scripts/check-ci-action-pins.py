#!/usr/bin/env python3
"""Fail when a third-party GitHub Action is not pinned to an immutable commit."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def main() -> int:
    violations: list[str] = []
    for workflow in sorted(WORKFLOW_ROOT.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for match in USES_RE.finditer(text):
            target = match.group(1)
            if target.startswith("./") or SHA_RE.fullmatch(target):
                continue
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{workflow}:{line}: {target}")
    if violations:
        print("GitHub Actions must be pinned to a 40-character commit SHA:")
        print("\n".join(violations))
        return 1
    print("All external GitHub Actions are pinned to immutable commit SHAs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
