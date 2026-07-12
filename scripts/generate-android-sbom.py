#!/usr/bin/env python3
"""Generate a deterministic CycloneDX SBOM for EchoDesk Android/TV assets."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
NPM_LOCK = ROOT / "desktop" / "package-lock.json"
PACKAGE_JSON = ROOT / "desktop" / "package.json"
GRADLE_VERIFICATION = ROOT / "desktop" / "android" / "gradle" / "verification-metadata.xml"
GRADLE_LOCKS = (
    ROOT / "desktop" / "android" / "app" / "gradle.lockfile",
    ROOT
    / "desktop"
    / "android"
    / "gradle"
    / "locks"
    / "capacitor-cordova-android-plugins.lockfile",
    ROOT / "desktop" / "android" / "gradle" / "locks" / "capacitor-android-8.4.0.lockfile",
)


def npm_components() -> list[dict[str, object]]:
    lock = json.loads(NPM_LOCK.read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for package_path, record in sorted((lock.get("packages") or {}).items()):
        if not package_path:
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
                "scope": "required" if record.get("dev") is not True else "excluded",
                "purl": f"pkg:npm/{encoded_name}@{quote(version)}",
                "bom-ref": f"pkg:npm/{name}@{version}",
            }
        )
    return components


def release_runtime_coordinates() -> set[tuple[str, str, str]]:
    coordinates: set[tuple[str, str, str]] = set()
    for lock_path in GRADLE_LOCKS:
        for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            coordinate, configurations = line.split("=", 1)
            if "releaseRuntimeClasspath" not in configurations.split(","):
                continue
            parts = coordinate.split(":")
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    f"invalid Gradle release runtime coordinate in {lock_path}: {coordinate}"
                )
            coordinates.add((parts[0], parts[1], parts[2]))
    if not coordinates:
        raise ValueError("Gradle locks contain no releaseRuntimeClasspath components")
    return coordinates


def gradle_components() -> list[dict[str, object]]:
    root = ET.parse(GRADLE_VERIFICATION).getroot()
    namespace = {"v": "https://schema.gradle.org/dependency-verification"}
    components: list[dict[str, object]] = []
    release_runtime = release_runtime_coordinates()
    discovered: set[tuple[str, str, str]] = set()
    for node in root.findall(".//v:component", namespace):
        group = node.attrib.get("group", "").strip()
        name = node.attrib.get("name", "").strip()
        version = node.attrib.get("version", "").strip()
        if not group or not name or not version:
            continue
        coordinate = (group, name, version)
        discovered.add(coordinate)
        hashes = sorted(
            {
                item.attrib["value"].lower()
                for item in node.findall(".//v:sha256", namespace)
                if len(item.attrib.get("value", "")) == 64
            }
        )
        ref = f"pkg:maven/{group}/{name}@{version}"
        component: dict[str, object] = {
            "type": "library",
            "group": group,
            "name": name,
            "version": version,
            "scope": "required" if coordinate in release_runtime else "optional",
            "purl": (
                f"pkg:maven/{quote(group, safe='.')}/{quote(name, safe='')}@"
                f"{quote(version, safe='.-_')}"
            ),
            "bom-ref": ref,
            "properties": [
                {
                    "name": "echodesk:gradle-scope",
                    "value": (
                        "releaseRuntimeClasspath"
                        if coordinate in release_runtime
                        else "resolved build/debug/test graph; not proven release runtime"
                    ),
                }
            ],
        }
        if hashes:
            component["hashes"] = [{"alg": "SHA-256", "content": value} for value in hashes]
        components.append(component)
    missing = sorted(release_runtime - discovered)
    if missing:
        raise ValueError(
            "Gradle verification metadata is missing release runtime components: "
            + ", ".join(":".join(coordinate) for coordinate in missing)
        )
    return components


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: generate-android-sbom.py OUTPUT", file=sys.stderr)
        return 2
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    components = sorted(
        [*gradle_components(), *npm_components()],
        key=lambda item: str(item["bom-ref"]),
    )
    properties = [
        {
            "name": "echodesk:npm-lock-sha256",
            "value": sha256(NPM_LOCK.read_bytes()).hexdigest(),
        },
        {
            "name": "echodesk:gradle-verification-sha256",
            "value": sha256(GRADLE_VERIFICATION.read_bytes()).hexdigest(),
        },
    ]
    properties.extend(
        {
            "name": f"echodesk:gradle-lock-sha256:{lock.relative_to(ROOT)}",
            "value": sha256(lock.read_bytes()).hexdigest(),
        }
        for lock in GRADLE_LOCKS
    )
    serial_seed = json.dumps(
        {
            "application": f"pkg:generic/echodesk-android@{package['version']}",
            "components": [item["bom-ref"] for item in components],
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
                "name": "EchoDesk Android and TV",
                "version": package["version"],
                "bom-ref": f"pkg:generic/echodesk-android@{package['version']}",
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
    print(f"Wrote {len(components)} Android/npm components to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
