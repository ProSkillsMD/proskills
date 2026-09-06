#!/usr/bin/env python3
"""Pipeline counts and status report for Oracle / operators.

Pairs with skills/pipeline-report.
dry-run = no writes; reads are OK if DB exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import DEFAULT_DB_PATH, connect_db, emit, exit_ok, iso_now


def _counts(conn) -> dict:
    def scalar(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    events = scalar("SELECT COUNT(*) FROM events")
    candidates_total = scalar("SELECT COUNT(*) FROM candidates")
    candidates_by_status = {
        r["status"]: r["c"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM candidates GROUP BY status"
        )
    }
    stage_runs_by = {
        f"{r['stage']}:{r['status']}": r["c"]
        for r in conn.execute(
            "SELECT stage, status, COUNT(*) AS c FROM stage_runs GROUP BY stage, status"
        )
    }
    pending_approvals = scalar(
        "SELECT COUNT(*) FROM approvals WHERE status IN ('pending', 'requested')"
    )
    outbound = scalar("SELECT COUNT(*) FROM outbound_events")
    outbound_by_status = {
        (r["status"] or "unknown"): r["c"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM outbound_events GROUP BY status"
        )
    }
    return {
        "events": events,
        "candidates_total": candidates_total,
        "candidates_by_status": candidates_by_status,
        "stage_runs_by_stage_status": stage_runs_by,
        "pending_approvals": pending_approvals,
        "outbound_events": outbound,
        "outbound_by_status": outbound_by_status,
    }


def run_report(
    *,
    db: Path | str = DEFAULT_DB_PATH,
    dry_run: bool = True,
    event_id: str | None = None,
    pipeline_context: dict[str, Any] | None = None,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Emit pipeline counts. Never writes. dry_run only affects the flag in output."""
    db_path = Path(db)

    empty_counts = {
        "events": 0,
        "candidates_total": 0,
        "candidates_by_status": {},
        "stage_runs_by_stage_status": {},
        "pending_approvals": 0,
        "outbound_events": 0,
    }

    if not db_path.exists():
        result = {
            "stage": "report",
            "status": "ok",
            "dry_run": dry_run,
            "message": "DB missing; empty report" if dry_run else "DB missing",
            "event_id": event_id,
            "counts": empty_counts,
            "summary": "empty",
            "action_required": False,
            "evidence_urls": [],
            "pipeline_context": pipeline_context,
        }
        if emit_output:
            emit(
                stage="report",
                status="ok",
                dry_run=dry_run,
                message=result["message"],
                event_id=event_id,
                counts=empty_counts,
                summary="empty",
                action_required=False,
                evidence_urls=[],
            )
        return result

    conn = connect_db(db_path)
    try:
        counts = _counts(conn)
        event_stages = None
        candidate = None
        if event_id:
            event_stages = {
                r["stage"]: r["status"]
                for r in conn.execute(
                    "SELECT stage, status FROM stage_runs WHERE event_id = ?",
                    (event_id,),
                )
            }
            crow = conn.execute(
                "SELECT event_id, name, status, local_path FROM candidates WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if crow:
                candidate = dict(crow)
    finally:
        conn.close()

    summary = (
        f"events={counts['events']} candidates={counts['candidates_total']} "
        f"pending_approvals={counts['pending_approvals']}"
    )
    result = {
        "stage": "report",
        "status": "ok",
        "dry_run": dry_run,
        "message": "Pipeline counts",
        "event_id": event_id,
        "counts": counts,
        "summary": summary,
        "item": None,
        "action_required": counts["pending_approvals"] > 0,
        "evidence_urls": [],
        "event_stages": event_stages,
        "candidate": candidate,
        "pipeline_context": pipeline_context,
        "timestamp": iso_now(),
    }
    if emit_output:
        emit(
            stage="report",
            status="ok",
            dry_run=dry_run,
            message="Pipeline counts",
            event_id=event_id,
            counts=counts,
            summary=summary,
            item=None,
            action_required=counts["pending_approvals"] > 0,
            evidence_urls=[],
            event_stages=event_stages,
            candidate=candidate,
            timestamp=result["timestamp"],
        )
    return result


def main() -> None:
    from _common import add_dry_run_apply_flags, resolve_dry_run

    parser = argparse.ArgumentParser(description="Emit pipeline counts report.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--event-id", default=None)
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    run_report(db=args.db, dry_run=resolve_dry_run(args), event_id=args.event_id, emit_output=True)
    exit_ok()


if __name__ == "__main__":
    main()
