"""内部 telemetry CLI，只暴露 k-suppressed aggregate 和安全操作 receipt。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.telemetry.contracts import (
    DEFAULT_K_THRESHOLD,
    DeletionReason,
    parse_telemetry_delete_request,
    parse_telemetry_query,
)
from app.telemetry.sqlite import SQLiteTelemetryAdapter

_DEFAULT_RETENTION_S = 30 * 24 * 60 * 60


def _timestamp_argument(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error


def _add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-at", type=_timestamp_argument)
    parser.add_argument("--end-at", type=_timestamp_argument)
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--key-version")
    parser.add_argument("--tenant-pseudonym")
    parser.add_argument("--operation")
    parser.add_argument("--platform")
    parser.add_argument("--app-version")
    parser.add_argument("--provider")
    parser.add_argument("--failure-reason")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EchoDesk internal telemetry aggregate CLI")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--retention-s", type=int, default=_DEFAULT_RETENTION_S)
    parser.add_argument("--k-threshold", type=int, default=DEFAULT_K_THRESHOLD)
    commands = parser.add_subparsers(dest="command", required=True)

    query = commands.add_parser("query", help="输出 k-suppressed aggregate JSON")
    _add_query_arguments(query)

    purge = commands.add_parser("purge", help="删除 retention window 外的事件")
    purge.add_argument("--now", type=_timestamp_argument)

    delete = commands.add_parser("delete", help="按伪名 identity scope 删除事件")
    delete.add_argument("--tenant-pseudonym")
    delete.add_argument("--user-pseudonym")
    delete.add_argument("--device-pseudonym")
    delete.add_argument("--key-version")
    delete.add_argument("--epoch", type=int)
    delete.add_argument("--reason", choices=[reason.value for reason in DeletionReason])
    return parser


def _query(args: argparse.Namespace) -> Any:
    fields = {
        name.replace("-", "_"): getattr(args, name.replace("-", "_"), None)
        for name in (
            "start_at",
            "end_at",
            "epoch",
            "key_version",
            "tenant_pseudonym",
            "operation",
            "platform",
            "app_version",
            "provider",
            "failure_reason",
        )
    }
    fields["k_threshold"] = args.k_threshold
    return parse_telemetry_query({key: value for key, value in fields.items() if value is not None})


def _delete_request(args: argparse.Namespace) -> Any:
    fields = {
        "tenant_pseudonym": args.tenant_pseudonym,
        "user_pseudonym": args.user_pseudonym,
        "device_pseudonym": args.device_pseudonym,
        "key_version": args.key_version,
        "epoch": args.epoch,
        "reason": args.reason,
    }
    return parse_telemetry_delete_request(
        {key: value for key, value in fields.items() if value is not None}
    )


def _json_output(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


async def _run(args: argparse.Namespace) -> None:
    adapter = SQLiteTelemetryAdapter(
        args.db,
        retention_s=args.retention_s,
        k_threshold=args.k_threshold,
    )
    if args.command == "query":
        aggregates = await adapter.query(_query(args))
        _json_output(
            {"aggregates": [aggregate.model_dump(mode="json") for aggregate in aggregates]}
        )
    elif args.command == "purge":
        purged = await adapter.purge_expired(now=args.now)
        _json_output({"purged_event_count": purged})
    else:
        receipt = await adapter.delete(_delete_request(args))
        _json_output({"deletion_receipt": receipt.model_dump(mode="json")})


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
