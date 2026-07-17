#!/usr/bin/env python3
"""Fail-closed production boundary gates for the embedded agent kernel.

The gate consumes an explicit source root, a frozen manifest, an import graph,
and a contract trace.  It never discovers a source root implicitly and never
starts a process, daemon, socket, or network client.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
SOURCE_EXTENSIONS = {".cjs", ".js", ".mjs", ".ts", ".tsx"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SNAPSHOT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

FORBIDDEN_MODULES = {
    "agentos",
    "agent-os",
    "@anthropic-ai/sdk",
    "child_process",
    "dgram",
    "dns",
    "electron",
    "fs",
    "fs/promises",
    "http",
    "https",
    "net",
    "node:child_process",
    "node:dgram",
    "node:dns",
    "node:fs",
    "node:fs/promises",
    "node:http",
    "node:https",
    "node:net",
    "node:tls",
    "node:worker_threads",
    "tls",
    "worker_threads",
}

FORBIDDEN_PATH_SEGMENTS = {
    "auth",
    "config",
    "daemon",
    "history",
    "telemetry",
    "update",
}

IMPORT_PATTERNS = (
    re.compile(r"\bimport\s+(?:type\s+)?(?:[^;\n]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
    re.compile(r"\bexport\s+(?:[^;\n]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
    re.compile(r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
)

FORBIDDEN_RUNTIME_PATTERNS = (
    ("LOCALHOST_OR_URL", re.compile(r"(?i)(?:https?|wss?)://|localhost|127\.0\.0\.1|\[::1\]|::1")),
    ("CHILD_PROCESS_CALL", re.compile(r"(?i)\b(?:spawn|exec|execFile|fork|bun\.spawn)\s*\(")),
    ("FILESYSTEM_CALL", re.compile(r"(?i)\b(?:readFile|writeFile|appendFile|mkdir|readdir|unlink|rm|rename|openSync|readFileSync|writeFileSync)\s*\(")),
    ("NETWORK_CALL", re.compile(r"(?i)\b(?:fetch|WebSocket|XMLHttpRequest|createServer|listen|connect)\s*\(")),
    ("GLOBAL_STATE_DISCOVERY", re.compile(r"(?i)(?:process\.env|os\.homedir|os\.userInfo|\.claude|settings\.json|credentials\.json|HOME|XDG_CONFIG_HOME)")),
    ("FORBIDDEN_RUNNER", re.compile(r"(?i)\b(?:agentos|agent[-_ ]?os|claude[-_ ]?(?:cli|code))\b")),
    ("FORBIDDEN_GLOBAL_SERVICE", re.compile(r"(?i)\b(?:global|user|home|xdg)[._/-]?(?:auth|config|history|update|telemetry)\b|\b(?:auth|config|history|update|telemetry)[._/-]?(?:store|manager|service|registry|db|file)\b")),
    ("DAEMON_REFERENCE", re.compile(r"(?i)\b(?:daemon|launchd|systemd)\b")),
    ("TELEMETRY_REFERENCE", re.compile(r"(?i)\b(?:telemetry|analytics|posthog|sentry|sendBeacon|trackEvent)\b")),
)


class GateFailure(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateFailure("INPUT_INVALID", f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise GateFailure("INPUT_INVALID", f"{label} must be an object")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GateFailure("FILE_READ_FAILED", str(path)) from exc
    return digest.hexdigest()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = copy.deepcopy(manifest)
    payload.pop("manifest_sha256", None)
    return _sha256_bytes(_canonical(payload))


def _source_manifest_digest(manifest: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical({
        "files": manifest.get("files"),
        "import_graph": manifest.get("import_graph"),
    }))


def _scan_imports(text: str) -> list[str]:
    found: set[str] = set()
    for pattern in IMPORT_PATTERNS:
        found.update(match.group(1) for match in pattern.finditer(text))
    return sorted(found)


def _module_is_forbidden(specifier: str) -> bool:
    normalized = specifier.replace("\\", "/").lower()
    if normalized in FORBIDDEN_MODULES:
        return True
    segments = [segment for segment in normalized.split("/") if segment]
    return any(segment in FORBIDDEN_PATH_SEGMENTS for segment in segments)


def _runtime_violations(text: str) -> list[str]:
    violations = []
    for code, pattern in FORBIDDEN_RUNTIME_PATTERNS:
        if pattern.search(text):
            violations.append(code)
    return violations


def _safe_relative(value: str, field: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise GateFailure("MANIFEST_PATH_INVALID", f"{field}: {value!r}")
    normalized = posixpath.normpath(value)
    if normalized != value or normalized == "." or normalized.startswith("../"):
        raise GateFailure("MANIFEST_PATH_INVALID", f"{field}: {value!r}")
    return normalized


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise GateFailure("SOURCE_ROOT_INVALID", str(root))
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GateFailure("SYMLINK_REJECTED", str(path))
        if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS:
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _resolve_import(root: Path, source_rel: str, specifier: str) -> str | None:
    if not specifier.startswith("."):
        return None
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), specifier))
    if candidate.startswith("../") or candidate == "..":
        raise GateFailure("IMPORT_TRAVERSAL", f"{source_rel} -> {specifier}")
    candidate_path = root / candidate
    candidates = [candidate_path]
    suffix = candidate_path.suffix.lower()
    if suffix == ".js":
        candidates.extend([candidate_path.with_suffix(ext) for ext in (".ts", ".tsx", ".mjs", ".cjs")])
    elif not suffix:
        candidates.extend(candidate_path.with_suffix(ext) for ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"))
    candidates.extend(candidate_path / f"index{ext}" for ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"))
    for resolved in candidates:
        if resolved.is_file() and not resolved.is_symlink():
            return resolved.relative_to(root).as_posix()
    raise GateFailure("UNKNOWN_IMPORT", f"{source_rel} -> {specifier}")


def _validate_identity(
    manifest: dict[str, Any],
    frozen: dict[str, Any],
    manifest_digest: str,
    source_manifest_digest: str,
) -> None:
    if frozen.get("schema_version") != SCHEMA_VERSION:
        raise GateFailure("IDENTITY_SCHEMA_MISMATCH", "frozen identity schema")
    for key in ("source_snapshot_id", "source_manifest_sha256", "echo_baseline_sha", "build_identity"):
        if key not in frozen:
            raise GateFailure("IDENTITY_MISSING", key)
        if manifest.get(key) != frozen[key]:
            raise GateFailure("IDENTITY_MISMATCH", key)
    source_snapshot = frozen["source_snapshot_id"]
    source_manifest = frozen["source_manifest_sha256"]
    echo_baseline = frozen["echo_baseline_sha"]
    if not isinstance(source_snapshot, str) or not SOURCE_SNAPSHOT_RE.fullmatch(source_snapshot):
        raise GateFailure("IDENTITY_INVALID", "source_snapshot_id")
    if not isinstance(source_manifest, str) or not SHA256_RE.fullmatch(source_manifest):
        raise GateFailure("IDENTITY_INVALID", "source_manifest_sha256")
    if source_manifest != source_manifest_digest:
        raise GateFailure("MANIFEST_TAMPERED", "source graph digest differs from frozen identity")
    if not isinstance(echo_baseline, str) or not SHA1_RE.fullmatch(echo_baseline):
        raise GateFailure("IDENTITY_INVALID", "echo_baseline_sha")
    if not isinstance(frozen["build_identity"], dict) or not frozen["build_identity"]:
        raise GateFailure("BUILD_IDENTITY_INVALID", "build_identity must be a non-empty object")


def validate_manifest(root: Path, manifest_path: Path, frozen_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path, "manifest")
    frozen = _load_json(frozen_path, "frozen identity")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise GateFailure("MANIFEST_SCHEMA_MISMATCH", "unsupported manifest schema")
    if manifest.get("kernel_contract") != "echo-agent-kernel/v1":
        raise GateFailure("CONTRACT_VERSION_MISMATCH", "kernel_contract")
    declared_digest = manifest.get("manifest_sha256")
    actual_digest = _manifest_digest(manifest)
    if declared_digest != actual_digest:
        raise GateFailure("MANIFEST_TAMPERED", "manifest_sha256 does not match canonical manifest bytes")
    source_digest = _source_manifest_digest(manifest)
    _validate_identity(manifest, frozen, actual_digest, source_digest)

    files_value = manifest.get("files")
    graph = manifest.get("import_graph")
    if not isinstance(files_value, list) or not isinstance(graph, dict):
        raise GateFailure("MANIFEST_INVALID", "files and import_graph are required")
    declared_files: dict[str, str] = {}
    for entry in files_value:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise GateFailure("MANIFEST_INVALID", "each file entry must contain path and sha256")
        rel = _safe_relative(str(entry["path"]), "file path")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise GateFailure("MANIFEST_INVALID", f"invalid hash for {rel}")
        if rel in declared_files:
            raise GateFailure("MANIFEST_INVALID", f"duplicate file {rel}")
        declared_files[rel] = digest

    actual_paths = {path.relative_to(root).as_posix(): path for path in _source_files(root)}
    if set(declared_files) != set(actual_paths):
        missing = sorted(set(declared_files) - set(actual_paths))
        extra = sorted(set(actual_paths) - set(declared_files))
        raise GateFailure("MANIFEST_FILE_CLOSURE", f"missing={missing}; extra={extra}")

    if set(graph) != set(declared_files):
        raise GateFailure("IMPORT_GRAPH_CLOSURE", "graph nodes do not equal manifest files")

    for rel, path in actual_paths.items():
        digest = _sha256_file(path)
        if digest != declared_files[rel]:
            raise GateFailure("FILE_TAMPERED", rel)
        text = path.read_text(encoding="utf-8")
        violations = _runtime_violations(text)
        if violations:
            raise GateFailure("FORBIDDEN_RUNTIME", f"{rel}: {','.join(violations)}")
        actual_imports = _scan_imports(text)
        declared_imports = graph[rel]
        if not isinstance(declared_imports, list) or any(not isinstance(item, str) for item in declared_imports):
            raise GateFailure("IMPORT_GRAPH_INVALID", rel)
        if sorted(set(declared_imports)) != actual_imports:
            raise GateFailure("IMPORT_GRAPH_DRIFT", f"{rel}: declared={declared_imports}; actual={actual_imports}")
        for specifier in actual_imports:
            if _module_is_forbidden(specifier):
                raise GateFailure("FORBIDDEN_IMPORT", f"{rel} -> {specifier}")
            resolved = _resolve_import(root, rel, specifier)
            if resolved is None:
                raise GateFailure("UNKNOWN_IMPORT", f"{rel} -> {specifier}")
            if resolved not in declared_files:
                raise GateFailure("IMPORT_CLOSURE", f"{rel} -> {specifier} -> {resolved}")

    return {
        "manifest_digest": actual_digest,
        "source_manifest_digest": source_digest,
        "files": len(actual_paths),
        "identity": frozen,
    }


def _checkpoint_checksum(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "checksum"}
    return f"sha256:{_sha256_bytes(_canonical(payload))}"


def validate_contract(contract_path: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    contract = _load_json(contract_path, "contract trace")
    if contract.get("schema_version") != SCHEMA_VERSION or contract.get("contract") != "echo-agent-kernel/v1":
        raise GateFailure("CONTRACT_VERSION_MISMATCH", "contract trace")
    if contract.get("identity") != {
        "source_snapshot_id": frozen["source_snapshot_id"],
        "echo_baseline_sha": frozen["echo_baseline_sha"],
        "build_identity": frozen["build_identity"],
    }:
        raise GateFailure("CONTRACT_IDENTITY_MISMATCH", "contract identity")
    cases = contract.get("cases")
    if not isinstance(cases, list) or not cases:
        raise GateFailure("CONTRACT_INVALID", "cases required")
    summaries = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("events"), list):
            raise GateFailure("CONTRACT_INVALID", "case events required")
        state = "new"
        terminal: str | None = None
        closed = False
        task_id = operation_key = request_id = None
        checkpoints = 0
        for index, event in enumerate(case["events"]):
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise GateFailure("CONTRACT_EVENT_INVALID", f"{case.get('name')}:#{index}")
            kind = event["type"]
            if closed:
                raise GateFailure("CONTRACT_ORDER_INVALID", f"{case.get('name')}: event after close")
            if kind == "open":
                if state != "new" or event.get("source_snapshot_id") != frozen["source_snapshot_id"] or event.get("build_identity") != frozen["build_identity"]:
                    raise GateFailure("OPEN_REJECTED", f"{case.get('name')}:#{index}")
                task_id, operation_key = event.get("task_id"), event.get("operation_key")
                if not isinstance(task_id, str) or not isinstance(operation_key, str) or not task_id or not operation_key:
                    raise GateFailure("OPEN_REJECTED", f"{case.get('name')}: identity missing")
                state = "open"
            elif kind == "run":
                if state not in {"open", "checkpointed"} or event.get("task_id") != task_id or event.get("operation_key") != operation_key:
                    raise GateFailure("RUN_REJECTED", f"{case.get('name')}:#{index}")
                request_id = event.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise GateFailure("RUN_REJECTED", f"{case.get('name')}: request_id missing")
                state = "running"
            elif kind == "checkpoint":
                if state != "running" or event.get("task_id") != task_id or event.get("operation_key") != operation_key or event.get("request_id") != request_id:
                    raise GateFailure("CHECKPOINT_REJECTED", f"{case.get('name')}:#{index}")
                if not isinstance(event.get("grant_revision"), int) or not isinstance(event.get("last_durable_event_seq"), int) or event["last_durable_event_seq"] < 1:
                    raise GateFailure("CHECKPOINT_REJECTED", f"{case.get('name')}: revision/sequence")
                if event.get("checksum") != _checkpoint_checksum(event):
                    raise GateFailure("CHECKPOINT_TAMPERED", f"{case.get('name')}:#{index}")
                checkpoints += 1
                state = "checkpointed"
            elif kind == "cancel":
                if state not in {"open", "running", "checkpointed"}:
                    raise GateFailure("CANCEL_REJECTED", f"{case.get('name')}:#{index}")
                terminal = "cancelled"
                state = "terminal"
            elif kind == "terminal":
                requested = event.get("state")
                if requested not in {"succeeded", "failed", "timeout", "cancelled"}:
                    raise GateFailure("TERMINAL_INVALID", f"{case.get('name')}:#{index}")
                if terminal is not None:
                    if event.get("late") is not True:
                        raise GateFailure("FIRST_TERMINAL_VIOLATION", f"{case.get('name')}:#{index}")
                    continue
                if state not in {"open", "running", "checkpointed", "terminal"}:
                    raise GateFailure("TERMINAL_REJECTED", f"{case.get('name')}:#{index}")
                terminal = requested
                state = "terminal"
            elif kind == "close":
                if state != "terminal" or terminal != event.get("state"):
                    raise GateFailure("CLOSE_REJECTED", f"{case.get('name')}:#{index}")
                closed = True
                state = "closed"
            else:
                raise GateFailure("CONTRACT_EVENT_UNKNOWN", f"{case.get('name')}: {kind}")
        if not closed or terminal is None:
            raise GateFailure("CONTRACT_INCOMPLETE", str(case.get("name")))
        summaries.append({"name": case.get("name", "unnamed"), "terminal": terminal, "checkpoints": checkpoints})
    return {"cases": summaries}


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    frozen = _load_json(Path(args.frozen_identity), "frozen identity")
    manifest_result = validate_manifest(Path(args.root), Path(args.manifest), Path(args.frozen_identity))
    contract_result = validate_contract(Path(args.contract), frozen) if args.contract else {"cases": []}
    return {"status": "PASS", "manifest": manifest_result, "contract": contract_result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="explicit kernel source root")
    parser.add_argument("--manifest", required=True, help="manifest containing file hashes and import graph")
    parser.add_argument("--frozen-identity", required=True, help="external frozen identity/build identity lock")
    parser.add_argument("--contract", help="typed lifecycle contract trace")
    parser.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = parser.parse_args(argv)
    try:
        result = run_gate(args)
    except GateFailure as exc:
        if args.json:
            print(json.dumps({"status": "FAIL", "code": exc.code, "detail": exc.detail}, ensure_ascii=False))
        else:
            print(f"FAIL {exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print(f"PASS files={result['manifest']['files']} cases={len(result['contract']['cases'])} manifest={result['manifest']['manifest_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
