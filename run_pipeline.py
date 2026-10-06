#!/usr/bin/env python3
"""Command-line entry point for the NBA coaching analytics pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.api_client import NBAApiClient
from src.database import connect_database, finish_run, start_run
from src.pipeline import Pipeline


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://nba-api-818216788681.us-central1.run.app"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest and normalize vendor NBA data into SQLite."
    )
    parser.add_argument(
        "--database", type=Path, default=PROJECT_ROOT / "output" / "nba_analytics.db",
        help="SQLite output path (default: output/nba_analytics.db)",
    )
    parser.add_argument(
        "--roster-as-of",
        help="ISO date assigned to the current roster snapshot (default: today)",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Delete only the specified database before loading a clean snapshot.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = os.environ.get("NBA_API_USERNAME")
    password = os.environ.get("NBA_API_PASSWORD")
    base_url = os.environ.get("NBA_API_BASE_URL", DEFAULT_BASE_URL)
    if not username or not password:
        print(
            "Set NBA_API_USERNAME and NBA_API_PASSWORD in the environment.",
            file=sys.stderr,
        )
        return 2
    if args.retries < 0:
        print("--retries must be non-negative.", file=sys.stderr)
        return 2

    database_path = args.database.expanduser().resolve()
    if args.rebuild and database_path.exists():
        # The target is fully resolved and limited to the path explicitly passed
        # by the operator; no directory or wildcard deletion is performed.
        database_path.unlink()

    connection = connect_database(database_path, PROJECT_ROOT / "schema.sql")
    run_id = start_run(connection, base_url)
    client = NBAApiClient(
        base_url, username, password, timeout=args.timeout, retries=args.retries
    )
    pipeline = Pipeline(
        client, connection, run_id, roster_as_of=args.roster_as_of
    )

    try:
        counts = pipeline.run()
        status = "completed_with_warnings" if pipeline.warning_count else "completed"
        finish_run(connection, run_id, status)
        print(json.dumps({
            "run_id": run_id,
            "status": status,
            "database": str(database_path),
            "counts": counts,
        }, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        connection.rollback()
        finish_run(connection, run_id, "failed")
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
