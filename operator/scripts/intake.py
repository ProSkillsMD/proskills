#!/usr/bin/env python3
"""Event intake with idempotency via event_id.

Replaces old Curio intake path; pairs with skills/intake-review.
Idempotency: duplicate event_id should skip or ack without double-processing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    PROJECT_ROOT,
    begin_immediate,
    connect_db,
    emit,
    ensure_schema,
    exit_fail,
    exit_ok,
    get_limits,
    iso_now,
    load_policies,
    resolve_under_root,
    upsert_stage_run,
)


def run_intake(
    event_id: str,
    *,
    source: str = "manual",
    name: str | None = None,
    source_uri: str | None = None,
    local_path: str | None = None,
    payload_json: str | None = None,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Ingest an event/candidate. Idempotent on event_id. Returns status dict."""
    event_id = (event_id or "").strip()
    db_path = Path(db)
    pol_path = Path(policies_path)

    if not event_id:
        result = {
            "stage": "intake",
            "status": "error",
            "dry_run": dry_run,
            "message": "event_id is required and must be non-empty",
            "event_id": event_id,
        }
        if emit_output:
            emit(
                stage="intake",
                status="error",
                dry_run=dry_run,
                message=result["message"],
                event_id=event_id,
            )
        return result

    resolved_local: str | None = None
    if local_path:
        try:
            resolved = resolve_under_root(PROJECT_ROOT, local_path)
            if not resolved.exists():
                result = {
                    "stage": "intake",
                    "status": "error",
                    "dry_run": dry_run,
                    "message": f"local-path does not exist: {local_path}",
                    "event_id": event_id,
                }
                if emit_output:
                    emit(
                        stage="intake",
                        status="error",
                        dry_run=dry_run,
                        message=result["message"],
                        event_id=event_id,
                    )
                return result
            resolved_local = str(resolved)
        except ValueError as exc:
            result = {
                "stage": "intake",
                "status": "error",
                "dry_run": dry_run,
                "message": str(exc),
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="intake",
                    status="error",
                    dry_run=dry_run,
                    message=str(exc),
                    event_id=event_id,
                )
            return result

    payload = payload_json
    if payload:
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            result = {
                "stage": "intake",
                "status": "error",
                "dry_run": dry_run,
                "message": f"invalid payload-json: {exc}",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="intake",
                    status="error",
                    dry_run=dry_run,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

    policies: dict = {}
    try:
        if pol_path.exists():
            policies = load_policies(pol_path)
    except Exception as exc:
        result = {
            "stage": "intake",
            "status": "error",
            "dry_run": dry_run,
            "message": f"failed to load policies: {exc}",
            "event_id": event_id,
        }
        if emit_output:
            emit(
                stage="intake",
                status="error",
                dry_run=dry_run,
                message=result["message"],
                event_id=event_id,
            )
        return result
    limits = get_limits(policies)
    _ = limits.get("batch_size", 10)

    item_name = name or event_id
    plan = {
        "event_id": event_id,
        "source": source,
        "name": item_name,
        "source_uri": source_uri,
        "local_path": resolved_local or local_path,
        "would_insert_event": True,
        "would_insert_candidate": True,
        "would_record_stage_run": "intake",
    }

    if dry_run:
        result = {
            "stage": "intake",
            "status": "ok",
            "dry_run": True,
            "message": "Would ingest event (no DB writes)",
            "event_id": event_id,
            "plan": plan,
            "item": item_name,
        }
        if emit_output:
            emit(
                stage="intake",
                status="ok",
                dry_run=True,
                message=result["message"],
                event_id=event_id,
                plan=plan,
                item=item_name,
            )
        return result

    now = iso_now()
    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        begin_immediate(conn)

        existing = conn.execute(
            "SELECT event_id, status FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()

        if existing:
            cand = conn.execute(
                "SELECT id, status FROM candidates WHERE event_id = ?", (event_id,)
            ).fetchone()
            upsert_stage_run(
                conn,
                event_id,
                "intake",
                "ok",
                summary=json.dumps(
                    {"note": "duplicate_ack", "candidate_id": cand["id"] if cand else None}
                ),
            )
            conn.commit()
            result = {
                "stage": "intake",
                "status": "duplicate",
                "dry_run": False,
                "message": "Event already ingested; no new candidate created",
                "event_id": event_id,
                "candidate_id": cand["id"] if cand else None,
                "item": item_name,
                "action_required": False,
            }
            if emit_output:
                emit(
                    stage="intake",
                    status="duplicate",
                    dry_run=False,
                    message=result["message"],
                    event_id=event_id,
                    candidate_id=result["candidate_id"],
                    item=item_name,
                    action_required=False,
                )
            return result

        conn.execute(
            """
            INSERT OR IGNORE INTO events (event_id, source, payload_json, received_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, source, payload, now, "received"),
        )
        if conn.execute("SELECT 1 FROM candidates WHERE event_id = ?", (event_id,)).fetchone():
            conn.commit()
            result = {
                "stage": "intake",
                "status": "duplicate",
                "dry_run": False,
                "message": "Candidate already exists for event_id",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="intake",
                    status="duplicate",
                    dry_run=False,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

        cur = conn.execute(
            """
            INSERT INTO candidates (event_id, name, source_uri, local_path, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                item_name,
                source_uri,
                resolved_local or local_path,
                "ingested",
                now,
            ),
        )
        candidate_id = cur.lastrowid
        upsert_stage_run(
            conn,
            event_id,
            "intake",
            "ok",
            summary=json.dumps({"candidate_id": candidate_id, "name": item_name}),
        )
        conn.commit()
        result = {
            "stage": "intake",
            "status": "ok",
            "dry_run": False,
            "message": "Event ingested",
            "event_id": event_id,
            "candidate_id": candidate_id,
            "item": item_name,
            "action_required": False,
        }
        if emit_output:
            emit(
                stage="intake",
                status="ok",
                dry_run=False,
                message=result["message"],
                event_id=event_id,
                candidate_id=candidate_id,
                item=item_name,
                action_required=False,
            )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Intake pipeline events (idempotent on event_id)."
    )
    parser.add_argument("--event-id", required=True, help="Stable event id (idempotency key)")
    parser.add_argument("--source", default="manual", help="Event source label")
    parser.add_argument("--name", default=None, help="Candidate name")
    parser.add_argument("--source-uri", default=None, help="Original source URI")
    parser.add_argument(
        "--local-path",
        default=None,
        help="Path to candidate directory under project root / fixtures",
    )
    parser.add_argument("--payload-json", default=None, help="Optional JSON payload string")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    from _common import add_dry_run_apply_flags, resolve_dry_run

    add_dry_run_apply_flags(parser)
    args = parser.parse_args()

    result = run_intake(
        args.event_id,
        source=args.source,
        name=args.name,
        source_uri=args.source_uri,
        local_path=args.local_path,
        payload_json=args.payload_json,
        db=args.db,
        policies_path=args.policies,
        dry_run=resolve_dry_run(args),
        emit_output=True,
    )
    if result.get("status") == "error":
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
