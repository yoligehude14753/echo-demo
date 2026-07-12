#!/usr/bin/env python3
"""Validate raw pip-audit evidence without suppressing accepted findings."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

PIN_RE_TEMPLATE = r"(?m)^{package}==([^\s;\\]+)"
EXPIRY_RE = re.compile(r"Exception expires:\s*(\d{4}-\d{2}-\d{2})")


def fail(message: str) -> None:
    raise ValueError(message)


def load_exit_code(path: Path) -> int:
    raw = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+", raw):
        fail(f"invalid pip-audit exit evidence in {path}: {raw!r}")
    return int(raw)


def load_report(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("dependencies"), list):
        fail(f"invalid pip-audit JSON schema in {path}")
    dependencies: list[dict[str, Any]] = []
    for index, item in enumerate(payload["dependencies"]):
        if not isinstance(item, dict):
            fail(f"dependency #{index} in {path} is not an object")
        if item.get("skip_reason"):
            fail(f"pip-audit skipped dependency {item.get('name', index)!r}: {item['skip_reason']}")
        if not isinstance(item.get("name"), str) or not isinstance(item.get("version"), str):
            fail(f"dependency #{index} in {path} has no resolved name/version")
        if not isinstance(item.get("vulns"), list):
            fail(f"dependency {item['name']!r} in {path} has no vulnerability list")
        dependencies.append(item)
    return dependencies


def findings(dependencies: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    result: list[tuple[str, str, dict[str, Any]]] = []
    for dependency in dependencies:
        for vulnerability in dependency["vulns"]:
            if not isinstance(vulnerability, dict):
                fail(f"invalid vulnerability entry for {dependency['name']!r}")
            result.append((dependency["name"], dependency["version"], vulnerability))
    return result


def locked_version(lock_path: Path, package: str) -> str:
    pattern = re.compile(PIN_RE_TEMPLATE.format(package=re.escape(package)))
    matches = pattern.findall(lock_path.read_text(encoding="utf-8"))
    versions = sorted(set(matches))
    if len(versions) != 1:
        fail(f"{lock_path} must pin exactly one {package} version; found {versions or 'none'}")
    return versions[0]


def validate_clean(report_path: Path, exit_path: Path) -> None:
    exit_code = load_exit_code(exit_path)
    discovered = findings(load_report(report_path))
    if exit_code != 0:
        fail(f"clean audit must exit 0, got {exit_code}")
    if discovered:
        summary = ", ".join(
            f"{name}=={version}:{vulnerability.get('id', 'missing-id')}"
            for name, version, vulnerability in discovered
        )
        fail(f"clean audit unexpectedly contains vulnerabilities: {summary}")


def validate_exception(
    report_path: Path,
    exit_path: Path,
    lock_path: Path,
    exception_path: Path,
    package: str,
    vulnerability_id: str,
) -> None:
    exit_code = load_exit_code(exit_path)
    discovered = findings(load_report(report_path))
    if exit_code != 1:
        fail(f"exception audit must preserve pip-audit exit 1, got {exit_code}")
    if len(discovered) != 1:
        fail(f"exception audit must contain exactly one finding, got {len(discovered)}")

    name, version, vulnerability = discovered[0]
    expected_version = locked_version(lock_path, package)
    identifiers = {str(vulnerability.get("id", ""))}
    aliases = vulnerability.get("aliases", [])
    if not isinstance(aliases, list):
        fail("pip-audit vulnerability aliases must be an array")
    identifiers.update(str(alias) for alias in aliases)
    if name != package or version != expected_version or vulnerability_id not in identifiers:
        fail(
            "unexpected exception finding: "
            f"{name}=={version} ids={sorted(identifier for identifier in identifiers if identifier)}; "
            f"expected {package}=={expected_version} containing {vulnerability_id}"
        )
    fix_versions = vulnerability.get("fix_versions")
    if fix_versions != []:
        fail(
            f"{vulnerability_id} now reports fixed versions {fix_versions!r}; "
            "remove or renew the exception instead of accepting it"
        )

    exception_text = exception_path.read_text(encoding="utf-8")
    if vulnerability_id not in exception_text:
        fail(f"{exception_path} does not name {vulnerability_id}")
    heading = re.search(
        rf"(?m)^##\s+.*{re.escape(vulnerability_id)}.*\b{re.escape(package)}\s+([^\s]+)\s*$",
        exception_text,
    )
    if heading is None or heading.group(1) != expected_version:
        fail(
            f"{exception_path} must bind {vulnerability_id} to "
            f"{package} {expected_version} in its heading"
        )
    expiry_match = EXPIRY_RE.search(exception_text)
    if expiry_match is None:
        fail(f"{exception_path} does not declare an exception expiry")
    expiry = date.fromisoformat(expiry_match.group(1))
    if date.today() > expiry:
        fail(f"{vulnerability_id} exception expired on {expiry.isoformat()}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="mode", required=True)

    clean = subparsers.add_parser("clean")
    clean.add_argument("--report", type=Path, required=True)
    clean.add_argument("--exit-code", type=Path, required=True)

    exception = subparsers.add_parser("exception")
    exception.add_argument("--report", type=Path, required=True)
    exception.add_argument("--exit-code", type=Path, required=True)
    exception.add_argument("--lock", type=Path, required=True)
    exception.add_argument("--exception", type=Path, required=True)
    exception.add_argument("--package", required=True)
    exception.add_argument("--vulnerability", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.mode == "clean":
            validate_clean(args.report, args.exit_code)
            print(f"Validated clean pip-audit evidence: {args.report}")
        else:
            validate_exception(
                args.report,
                args.exit_code,
                args.lock,
                args.exception,
                args.package,
                args.vulnerability,
            )
            print(
                f"Validated one explicit pip-audit exception: {args.package} {args.vulnerability}"
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"pip-audit evidence rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
