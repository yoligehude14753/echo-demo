#!/usr/bin/env python3
"""Validate an explicit, time-bounded npm audit exception.

The checker keeps the raw non-zero audit result and accepts only the reviewed
image-size findings that currently have no non-breaking pptxgenjs fix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

EXPIRY_RE = re.compile(r"Exception expires:\s*(\d{4}-\d{2}-\d{2})")
ADVISORY_RE = re.compile(r"GHSA-[a-z0-9-]+", re.IGNORECASE)


def fail(message: str) -> None:
    raise ValueError(message)


def load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    vulnerabilities = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(vulnerabilities, dict):
        fail(f"npm audit JSON has no vulnerabilities object: {path}")
    return payload


def load_exit_code(path: Path) -> int:
    raw = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+", raw):
        fail(f"invalid npm audit exit evidence in {path}: {raw!r}")
    return int(raw)


def advisory_ids(entry: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    via = entry.get("via", [])
    if not isinstance(via, list):
        fail("npm audit via must be an array")
    for item in via:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str):
                result.update(match.upper() for match in ADVISORY_RE.findall(url))
        elif isinstance(item, str):
            # Dependency-name edges are not vulnerability identities.
            continue
        else:
            fail("npm audit via entry must be an object or dependency name")
    return result


def exception_section(text: str, advisory: str) -> str:
    match = re.search(rf"(?im)^##\s+.*{re.escape(advisory)}.*$", text)
    if match is None:
        fail(f"exception file has no section for {advisory}")
    rest = text[match.end() :]
    next_heading = re.search(r"(?m)^##\s+", rest)
    return text[match.start() : match.end() + (next_heading.start() if next_heading else len(rest))]


def validate(args: argparse.Namespace) -> None:
    if load_exit_code(args.exit_code) != 1:
        fail("npm exception audit must preserve exit code 1")
    report = load_report(args.report)
    vulnerabilities = report["vulnerabilities"]
    expected_packages = {"image-size", args.root_package}
    if set(vulnerabilities) != expected_packages:
        fail(
            "npm audit exception contains unexpected packages: "
            f"{sorted(vulnerabilities)}; expected {sorted(expected_packages)}"
        )

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        fail(f"package-lock has no packages object: {args.lock}")
    root = packages.get(f"node_modules/{args.root_package}")
    if not isinstance(root, dict) or root.get("version") != args.root_version:
        fail(f"{args.root_package} must be locked at {args.root_version}")

    image = vulnerabilities["image-size"]
    if not isinstance(image, dict):
        fail("image-size vulnerability entry is not an object")
    image_version = packages.get("node_modules/image-size", {}).get("version")
    if image_version != args.image_version:
        fail(f"image-size must be locked at {args.image_version}, got {image_version!r}")
    discovered = advisory_ids(image)
    expected_advisories = {item.upper() for item in args.advisory}
    if discovered != expected_advisories:
        fail(f"unexpected image-size advisories: {sorted(discovered)}")

    root_entry = vulnerabilities[args.root_package]
    if not isinstance(root_entry, dict):
        fail(f"{args.root_package} vulnerability entry is not an object")
    fix = root_entry.get("fixAvailable")
    if fix != {"name": args.root_package, "version": "1.1.5", "isSemVerMajor": True}:
        fail(f"reviewed breaking-only fix changed: {fix!r}")

    exception_text = args.exception.read_text(encoding="utf-8")
    for advisory in sorted(expected_advisories):
        section = exception_section(exception_text, advisory)
        expiry_match = EXPIRY_RE.search(section)
        if expiry_match is None:
            fail(f"{advisory} exception has no expiry")
        expiry = date.fromisoformat(expiry_match.group(1))
        if date.today() > expiry:
            fail(f"{advisory} exception expired on {expiry.isoformat()}")
        if args.root_package not in section or args.root_version not in section:
            fail(f"{advisory} section is not bound to {args.root_package} {args.root_version}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--report", type=Path, required=True)
    result.add_argument("--exit-code", type=Path, required=True)
    result.add_argument("--lock", type=Path, required=True)
    result.add_argument("--exception", type=Path, required=True)
    result.add_argument("--root-package", required=True)
    result.add_argument("--root-version", required=True)
    result.add_argument("--image-version", required=True)
    result.add_argument("--advisory", action="append", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate(args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"npm audit evidence rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Validated explicit npm audit exception: {args.root_package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
