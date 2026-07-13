#!/usr/bin/env python3
"""Generate a deterministic CycloneDX SBOM from committed Python/npm locks."""

from __future__ import annotations

import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
PYTHON_LOCK = ROOT / "backend" / "requirements.lock"
DESKTOP_NPM_LOCK = ROOT / "desktop" / "package-lock.json"
PPT_RUNTIME_ROOT = ROOT / "backend" / "app" / "adapters" / "skill" / "assets" / "ppt_ib_deck"
PPT_RUNTIME_NPM_LOCK = PPT_RUNTIME_ROOT / "package-lock.json"
PPT_RUNTIME_PACKAGE_JSON = PPT_RUNTIME_ROOT / "package.json"
PACKAGE_JSON = ROOT / "desktop" / "package.json"
PYTHON_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", re.MULTILINE)
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def normalized_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_components() -> list[dict[str, object]]:
    text = PYTHON_LOCK.read_text(encoding="utf-8")
    components: list[dict[str, object]] = []
    matches = list(PYTHON_PIN_RE.finditer(text))
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start() : block_end]
        name = normalized_python_name(match.group(1))
        version = match.group(2)
        hashes = sorted(set(HASH_RE.findall(block)))
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
                "bom-ref": f"pkg:pypi/{name}@{version}",
                "hashes": [{"alg": "SHA-256", "content": value} for value in hashes],
            }
        )
    return components


def npm_lock_components(
    lock_path: Path,
    *,
    force_required: bool = False,
) -> list[dict[str, object]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(f"npm lock has no packages object: {lock_path}")
    components: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for package_path, record in sorted(packages.items()):
        if not package_path or not isinstance(record, dict):
            continue
        name = record.get("name")
        version = record.get("version")
        if not isinstance(name, str):
            marker = "node_modules/"
            if marker not in package_path:
                continue
            name = package_path.rsplit(marker, 1)[-1]
        if not isinstance(version, str) or (name, version) in seen:
            continue
        seen.add((name, version))
        encoded_name = "/".join(quote(part, safe="") for part in name.split("/"))
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "scope": (
                    "required"
                    if force_required or record.get("dev") is not True or name == "electron"
                    else "excluded"
                ),
                "purl": f"pkg:npm/{encoded_name}@{quote(version)}",
                "bom-ref": f"pkg:npm/{name}@{version}",
            }
        )
    return components


def npm_components() -> list[dict[str, object]]:
    desktop_components = npm_lock_components(DESKTOP_NPM_LOCK)
    ppt_components = npm_lock_components(PPT_RUNTIME_NPM_LOCK, force_required=True)

    ppt_package = json.loads(PPT_RUNTIME_PACKAGE_JSON.read_text(encoding="utf-8"))
    ppt_lock = json.loads(PPT_RUNTIME_NPM_LOCK.read_text(encoding="utf-8"))
    direct_dependencies = ppt_package.get("dependencies")
    if not isinstance(direct_dependencies, dict) or not direct_dependencies:
        raise ValueError("packaged PPT runtime must declare direct dependencies")
    locked_direct_dependencies = (ppt_lock.get("packages") or {}).get("", {}).get("dependencies")
    if locked_direct_dependencies != direct_dependencies:
        raise ValueError("packaged PPT runtime lock does not match package dependencies")
    locked_ppt = {
        (str(component["name"]), str(component["version"])) for component in ppt_components
    }
    for name, requirement in sorted(direct_dependencies.items()):
        if not isinstance(name, str) or not isinstance(requirement, str):
            raise ValueError("packaged PPT runtime dependency names/versions must be strings")
        resolved = (ppt_lock.get("packages") or {}).get(f"node_modules/{name}", {}).get("version")
        if not isinstance(resolved, str) or (name, resolved) not in locked_ppt:
            raise ValueError(
                f"packaged PPT runtime dependency missing from lock: {name} ({requirement})"
            )

    merged: dict[str, dict[str, object]] = {}
    for component in [*desktop_components, *ppt_components]:
        reference = str(component["bom-ref"])
        existing = merged.get(reference)
        if existing is None:
            merged[reference] = component
        elif component.get("scope") == "required":
            existing["scope"] = "required"
    return list(merged.values())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: generate-release-sbom.py OUTPUT", file=sys.stderr)
        return 2
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    components = sorted(
        [*python_components(), *npm_components()],
        key=lambda item: str(item["bom-ref"]),
    )
    properties = [
        {
            "name": "echodesk:python-lock-sha256",
            "value": sha256(PYTHON_LOCK.read_bytes()).hexdigest(),
        },
        {
            "name": "echodesk:desktop-npm-lock-sha256",
            "value": sha256(DESKTOP_NPM_LOCK.read_bytes()).hexdigest(),
        },
        {
            "name": "echodesk:ppt-runtime-npm-lock-sha256",
            "value": sha256(PPT_RUNTIME_NPM_LOCK.read_bytes()).hexdigest(),
        },
    ]
    serial_seed = json.dumps(
        {
            "application": f"pkg:generic/echodesk@{package['version']}",
            "components": components,
            "inputs": properties,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{UUID(hex=sha256(serial_seed).hexdigest()[:32])}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": package.get("productName", "EchoDesk"),
                "version": package["version"],
                "bom-ref": f"pkg:generic/echodesk@{package['version']}",
            },
            "properties": properties,
        },
        "components": components,
    }
    output = Path(argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(components)} components to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
