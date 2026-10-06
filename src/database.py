"""SQLite setup and small persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_database(path: Path, schema_path: Path) -> sqlite3.Connection:
    """Open a database, enforce integrity, and apply the idempotent DDL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    return connection


def start_run(connection: sqlite3.Connection, base_url: str) -> int:
    cursor = connection.execute(
        "INSERT INTO pipeline_runs(started_at, status, base_url) VALUES (?, 'running', ?)",
        (utc_now(), base_url),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(connection: sqlite3.Connection, run_id: int, status: str) -> None:
    connection.execute(
        "UPDATE pipeline_runs SET completed_at = ?, status = ? WHERE run_id = ?",
        (utc_now(), status, run_id),
    )
    connection.commit()


def log_fetch(
    connection: sqlite3.Connection,
    run_id: int,
    endpoint: str,
    *,
    status: Optional[int],
    success: bool,
    record_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO api_fetch_log(
            run_id, endpoint, fetched_at, http_status, success,
            record_count, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            endpoint,
            utc_now(),
            status,
            int(success),
            record_count,
            error_message,
        ),
    )
    connection.commit()


def log_quality_issue(
    connection: sqlite3.Connection,
    run_id: int,
    endpoint: str,
    entity_type: str,
    issue_code: str,
    details: str,
    *,
    source_key: Optional[str] = None,
    severity: str = "warning",
    raw_record: Optional[Any] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO data_quality_issues(
            run_id, endpoint, entity_type, source_key, issue_code,
            severity, details, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            endpoint,
            entity_type,
            source_key,
            issue_code,
            severity,
            details,
            json.dumps(raw_record, sort_keys=True) if raw_record is not None else None,
        ),
    )


def infer_record_count(payload: Any) -> Optional[int]:
    """Count records across the API's inconsistent response envelopes."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("players", "games", "data", "rosters", "injuries"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        if "homeTeam" in payload and "awayTeam" in payload:
            return sum(
                len(payload.get(side, {}).get("players", []))
                for side in ("homeTeam", "awayTeam")
            )
    return None
