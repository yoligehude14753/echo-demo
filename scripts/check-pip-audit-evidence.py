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
LOCAL_PIN_RE = re.compile(
    r"(?m)^([A-Za-z0-9_.-]+)==([^\s;+\\]+)\+([^\s;\\]+)(?=\s*(?:;|\\|$))"
)
AUDITABLE_LOCAL_BUILDS = {"torch": "cpu", "torchaudio": "cpu"}


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


def public_version(version: str) -> str:
    """Strip a PEP 440 local build suffix without weakening the base pin."""

    return version.split("+", 1)[0]


def locked_versions(lock_path: Path, package: str) -> tuple[str, set[str]]:
    pattern = re.compile(PIN_RE_TEMPLATE.format(package=re.escape(package)))
    matches = pattern.findall(lock_path.read_text(encoding="utf-8"))
    versions = set(matches)
    public_versions = {public_version(version) for version in versions}
    if len(public_versions) != 1:
        fail(
            f"{lock_path} must pin one public {package} version; "
            f"found {sorted(versions) or 'none'}"
        )
    return next(iter(public_versions)), versions


def normalize_lock_for_audit(lock_path: Path, output_path: Path) -> None:
    """Map reviewed CPU wheel local versions to their advisory public version.

    pip-audit's PyPI vulnerability service does not index ``+cpu`` local build
    labels.  The exact hashed install lock remains untouched and is still the
    evidence authority; this derived copy is retained beside the raw report.
    """

    text = lock_path.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        package, base, local = match.groups()
        normalized_package = package.lower().replace("_", "-")
        if AUDITABLE_LOCAL_BUILDS.get(normalized_package) != local:
            fail(f"unreviewed local dependency build: {package}=={base}+{local}")
        return f"{package}=={base}"

    normalized, count = LOCAL_PIN_RE.subn(replace, text)
    if "+cpu" in text and count == 0:
        fail(f"{lock_path} contains an unrecognized CPU local-version pin")
    output_path.write_text(normalized, encoding="utf-8")


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
    expected_findings: list[tuple[str, str]],
) -> None:
    exit_code = load_exit_code(exit_path)
    discovered = findings(load_report(report_path))
    if exit_code != 1:
        fail(f"exception audit must preserve pip-audit exit 1, got {exit_code}")
    if len(discovered) != len(expected_findings):
        fail(
            f"exception audit must contain exactly {len(expected_findings)} findings, "
            f"got {len(discovered)}"
        )
    exception_text = exception_path.read_text(encoding="utf-8")
    unmatched = list(discovered)
    for package, vulnerability_id in expected_findings:
        matches: list[tuple[str, str, dict[str, Any]]] = []
        for finding in unmatched:
            name, _version, vulnerability = finding
            identifiers = {str(vulnerability.get("id", ""))}
            aliases = vulnerability.get("aliases", [])
            if not isinstance(aliases, list):
                fail("pip-audit vulnerability aliases must be an array")
            identifiers.update(str(alias) for alias in aliases)
            if name == package and vulnerability_id in identifiers:
                matches.append(finding)
        if len(matches) != 1:
            fail(
                f"expected exactly one {package}/{vulnerability_id} finding, "
                f"got {len(matches)}"
            )

        name, version, vulnerability = matches[0]
        unmatched.remove(matches[0])
        expected_version, accepted_versions = locked_versions(lock_path, package)
        identifiers = {str(vulnerability.get("id", ""))}
        aliases = vulnerability.get("aliases", [])
        if not isinstance(aliases, list):
            fail("pip-audit vulnerability aliases must be an array")
        identifiers.update(str(alias) for alias in aliases)
        version_matches_lock = version == expected_version or version in accepted_versions
        if name != package or not version_matches_lock or vulnerability_id not in identifiers:
            fail(
                "unexpected exception finding: "
                f"{name}=={version} ids={sorted(identifier for identifier in identifiers if identifier)}; "
                f"expected {package} in {sorted(accepted_versions)} "
                f"(public {expected_version}) containing {vulnerability_id}"
            )

        heading = re.search(
            rf"(?m)^##\s+.*{re.escape(vulnerability_id)}.*\b{re.escape(package)}\s+([^\s]+)\s*$",
            exception_text,
        )
        if heading is None or heading.group(1) != expected_version:
            fail(
                f"{exception_path} must bind {vulnerability_id} to "
                f"{package} {expected_version} in its heading"
            )
        next_heading = re.search(r"(?m)^##\s+", exception_text[heading.end() :])
        section_end = heading.end() + next_heading.start() if next_heading else len(exception_text)
        section = exception_text[heading.start() : section_end]
        expiry_match = EXPIRY_RE.search(section)
        if expiry_match is None:
            fail(f"{exception_path} does not declare an exception expiry for {vulnerability_id}")
        expiry = date.fromisoformat(expiry_match.group(1))
        if date.today() > expiry:
            fail(f"{vulnerability_id} exception expired on {expiry.isoformat()}")

        fix_versions = vulnerability.get("fix_versions")
        if fix_versions != []:
            if (
                package != "setuptools"
                or vulnerability_id != "CVE-2026-59890"
                or fix_versions != ["83.0.0"]
                or "setuptools>=83.0.0" not in section
            ):
                fail(
                    f"{vulnerability_id} now reports fixed versions {fix_versions!r}; "
                    "remove or renew the exception instead of accepting it"
                )


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
    exception.add_argument("--package", action="append", required=True)
    exception.add_argument("--vulnerability", action="append", required=True)
    normalize = subparsers.add_parser("normalize-lock")
    normalize.add_argument("--lock", type=Path, required=True)
    normalize.add_argument("--output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.mode == "normalize-lock":
            normalize_lock_for_audit(args.lock, args.output)
            print(f"Prepared advisory audit input: {args.output}")
        elif args.mode == "clean":
            validate_clean(args.report, args.exit_code)
            print(f"Validated clean pip-audit evidence: {args.report}")
        else:
            if len(args.package) != len(args.vulnerability):
                fail("--package and --vulnerability must be supplied the same number of times")
            validate_exception(
                args.report,
                args.exit_code,
                args.lock,
                args.exception,
                list(zip(args.package, args.vulnerability)),
            )
            print(f"Validated explicit pip-audit exceptions: {len(args.package)}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"pip-audit evidence rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
