#!/usr/bin/env python3
"""Create / migrate the ProSkills pipeline SQLite schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Allow running as scripts/init_state.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_STATE_ROOT,
    SCHEMA_SQL,
    StateRootError,
    add_state_root_arg,
    connect_db,
    emit,
    ensure_schema,
    ensure_state_dirs,
    exit_ok,
    get_state_paths,
)

TABLES = [
    "events",
    "candidates",
    "stage_runs",
    "artifacts",
    "approvals",
    "outbound_events",
]
INDEXES = [
    "idx_candidates_event_id",
    "idx_stage_runs_event_id",
    "idx_stage_runs_stage",
    "idx_artifacts_event_id",
    "idx_approvals_event_id",
    "idx_outbound_event_id",
    "idx_outbound_key",
    "idx_outbound_status",
]


def run_init_state(
    *,
    db: Path | str = DEFAULT_DB_PATH,
    state_root: Path | str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Initialize schema. dry_run=True creates no file."""
    db_path = Path(db)
    explicit_state_root = state_root is not None
    try:
        paths = get_state_paths(
            state_root if explicit_state_root else DEFAULT_STATE_ROOT
        )
    except StateRootError as exc:
        result = {
            "stage": "init_state",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "db": str(db_path),
        }
        if emit_output:
            emit(
                stage="init_state",
                status="error",
                dry_run=dry_run,
                message=str(exc),
                db=str(db_path),
            )
        return result

    if dry_run:
        result = {
            "stage": "init_state",
            "status": "ok",
            "dry_run": True,
            "message": "Would create DB and schema",
            "db": str(db_path),
            "state_root": str(paths.state_root),
            "would_create_tables": TABLES,
            "would_create_indexes": INDEXES,
            "would_create_dirs": ["artifacts", "locks", "logs"],
            "schema_preview": SCHEMA_SQL.strip()[:500] + "...",
        }
        if emit_output:
            emit(
                stage="init_state",
                status="ok",
                dry_run=True,
                message=result["message"],
                db=str(db_path),
                would_create_tables=TABLES,
                would_create_indexes=INDEXES,
                schema_preview=result["schema_preview"],
            )
        return result

    if explicit_state_root:
        ensure_state_dirs(paths)
    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        existing = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()

    result = {
        "stage": "init_state",
        "status": "ok",
        "dry_run": False,
        "message": "Schema created or already present",
        "db": str(db_path),
        "state_root": str(paths.state_root),
        "tables": existing,
    }
    if emit_output:
        emit(
            stage="init_state",
            status="ok",
            dry_run=False,
            message=result["message"],
            db=str(db_path),
            tables=existing,
        )
    return result


def main() -> None:
    from _common import add_dry_run_apply_flags, resolve_dry_run

    parser = argparse.ArgumentParser(description="Initialize pipeline SQLite schema.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite DB path")
    add_state_root_arg(parser)
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    run_init_state(
        db=args.db,
        state_root=args.state_root,
        dry_run=resolve_dry_run(args),
        emit_output=True,
    )
    exit_ok()


if __name__ == "__main__":
    main()
