#!/usr/bin/env python3
"""F02 task-owned canonical trace validator.

The validator checks trace shape and semantic invariants only. It does not claim
that the current RC has executed the candidate traces; unresolved gaps produce
an honest SEMANTIC_BLOCKED verdict.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CASES = {"success", "one-tool-call", "permission-denied", "cancel", "compact/resume"}
CRITICAL_GAPS = {
    "F02-G01",
    "F02-G06",
    "F02-G07",
    "F02-G08",
    "F02-G13",
    "F02-G15",
}


def load_traces(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"non-object trace in {path}")
    return rows


def check_identity(row: dict[str, Any]) -> list[str]:
    identity = row.get("identity")
    if not isinstance(identity, dict):
        return ["identity missing"]
    required = {"task_id", "operation_key", "request_id"}
    return [f"identity.{key} missing" for key in required if not identity.get(key)]


def check_events(row: dict[str, Any]) -> list[str]:
    events = row.get("canonical_events")
    if not isinstance(events, list) or not events:
        return ["canonical_events missing"]
    seqs = [event.get("seq") for event in events]
    errors = []
    if seqs != list(range(1, len(seqs) + 1)):
        errors.append("canonical seq is not contiguous")
    terminal = row.get("terminal", {}).get("event")
    if events[-1].get("event") != terminal:
        errors.append("terminal is not the final canonical event")
    if not row.get("native_events"):
        errors.append("native_events missing")
    native_seqs = [event.get("seq") for event in row.get("native_events", [])]
    if native_seqs and native_seqs != list(range(1, len(native_seqs) + 1)):
        errors.append("native seq is not contiguous")
    return errors


def check_case(row: dict[str, Any]) -> list[str]:
    case = row.get("case")
    events = [event.get("event") for event in row.get("canonical_events", [])]
    errors: list[str] = []
    if case == "one-tool-call":
        correlation = row.get("correlation", {})
        if correlation.get("tool_use_id") != correlation.get("tool_result_id"):
            errors.append("tool correlation mismatch")
        if "tool.requested" not in events or "tool.completed" not in events:
            errors.append("tool request/result pair missing")
    if case == "permission-denied":
        if "permission.denied" not in events:
            errors.append("permission denial missing")
        if "tool.started" in events:
            errors.append("denied tool started")
        if events.index("permission.denied") < events.index("permission.requested"):
            errors.append("permission denied precedes request")
    if case == "cancel":
        if "cancel.requested" not in events or not events[-1].endswith("cancelled"):
            errors.append("cancel terminal sequence missing")
    if case == "compact/resume":
        required = {"compaction.started", "compaction.completed", "checkpoint.created", "resume.started", "resume.completed"}
        errors.extend(f"compact event missing: {event}" for event in sorted(required - set(events)))
        if events.index("resume.started") < events.index("checkpoint.created"):
            errors.append("resume starts before checkpoint")
    return errors


def validate(rows: list[dict[str, Any]], source: str) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("source") != source:
            errors.append(f"source mismatch: {row.get('trace_id')}")
        case = row.get("case")
        if case in seen:
            errors.append(f"duplicate case: {case}")
        seen.add(case)
        errors.extend(f"{row.get('trace_id')}: {error}" for error in check_identity(row))
        errors.extend(f"{row.get('trace_id')}: {error}" for error in check_events(row))
        errors.extend(f"{row.get('trace_id')}: {error}" for error in check_case(row))
    errors.extend(f"missing case: {case}" for case in sorted(CASES - seen))
    gaps = CRITICAL_GAPS | {gap for row in rows for gap in row.get("gap_ids", [])}
    return errors, gaps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--echo", type=Path, required=True)
    parser.add_argument("--require-compatible", action="store_true")
    args = parser.parse_args()
    claude_errors, claude_gaps = validate(load_traces(args.claude), "claude")
    echo_errors, echo_gaps = validate(load_traces(args.echo), "echo")
    errors = claude_errors + echo_errors
    critical = sorted((claude_gaps | echo_gaps) & CRITICAL_GAPS)
    verdict = "SEMANTIC_BLOCKED" if critical else "INTERFACE_COMPATIBLE"
    result = {"structural_valid": not errors, "verdict": verdict, "critical_gaps": critical, "errors": errors}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if errors or (args.require_compatible and verdict != "INTERFACE_COMPATIBLE"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
