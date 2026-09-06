#!/usr/bin/env python3
"""Orchestrate ProSkills stages: init_state → intake → validate → static_scan → report.

Default is dry-run (no local state writes). Pass --apply for real writes.
Never executes submitted candidate code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    DEFAULT_STATE_ROOT,
    EventLockHeld,
    StateRootError,
    add_state_root_arg,
    connect_db,
    emit,
    emit_json,
    ensure_state_dirs,
    event_lock,
    exit_fail,
    exit_ok,
    get_ai_escalation,
    get_artifact_retention,
    get_stage_run,
    get_state_paths,
    iso_now,
    load_policies,
)
from init_state import run_init_state
from intake import run_intake
from report import run_report
from static_scan import run_static_scan
from validate import run_validate

STAGES = ("init_state", "intake", "validate", "static_scan", "report")

# Statuses that mean a stage completed successfully enough to advance
OK_STATUSES = frozenset({"ok", "duplicate"})

# Statuses that permanently block later non-report stages
BLOCKING_STATUSES = frozenset(
    {"rejected", "critical", "quarantined", "error", "validate_failed", "blocked"}
)


def _stage_status(conn, event_id: str, stage: str) -> str | None:
    row = get_stage_run(conn, event_id, stage)
    return row["status"] if row else None


def _should_skip_completed(status: str | None) -> bool:
    return status in OK_STATUSES


def run_pipeline(
    event_id: str,
    *,
    name: str | None = None,
    source: str = "manual",
    source_uri: str | None = None,
    local_path: str | None = None,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    state_root: Path | str | None = None,
    locks_dir: Path | str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Run ordered stages with locking, resume, and fail-closed blocking."""
    event_id = (event_id or "").strip()
    db_path = Path(db)
    pol_path = Path(policies_path)
    try:
        paths = get_state_paths(
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )
    except StateRootError as exc:
        out = {
            "stage": "pipeline",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(out)
        return out
    # Locks always from state-root unless explicit locks_dir under same root
    lock_dir = Path(locks_dir) if locks_dir else paths.locks_dir
    if not dry_run:
        ensure_state_dirs(paths)

    if not event_id:
        out = {
            "stage": "pipeline",
            "status": "error",
            "dry_run": dry_run,
            "message": "event_id is required",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(out)
        return out

    policies = load_policies(pol_path) if pol_path.exists() else {}
    retention = get_artifact_retention(policies)
    ai_esc = get_ai_escalation(policies)

    stages_result: dict[str, Any] = {}
    blocked_reason: str | None = None
    overall = "ok"

    def record(stage: str, action: str, payload: dict | None = None, reason: str | None = None) -> None:
        stages_result[stage] = {
            "action": action,
            "status": (payload or {}).get("status"),
            "reason": reason,
            "result": payload,
        }

    # Acquire lock for apply path (and also dry-run for consistency when coordinating)
    try:
        lock_cm = event_lock(event_id, lock_dir, blocking=False)
        lock_cm.__enter__()
    except EventLockHeld as exc:
        out = {
            "stage": "pipeline",
            "status": "locked",
            "dry_run": dry_run,
            "message": f"Another process holds the lock for event_id={event_id}",
            "event_id": event_id,
            "lock_path": str(exc.lock_path),
            "timestamp": iso_now(),
            "artifact_retention": retention,
            "ai_escalation": ai_esc,
            "stages": {},
        }
        if emit_output:
            emit(
                stage="pipeline",
                status="locked",
                dry_run=dry_run,
                message=out["message"],
                event_id=event_id,
                lock_path=str(exc.lock_path),
            )
        return out

    try:
        # --- init_state ---
        # In dry-run we do not create DB; later stages that need DB will be simulated
        # or report empty. For dry-run orchestration we still "run" each stage in dry mode.
        if dry_run:
            # Check if DB already exists for resume-style dry simulation of later stages
            init_res = run_init_state(db=db_path, state_root=paths.state_root, dry_run=True, emit_output=False)
            record("init_state", "ran", init_res)
        else:
            # Ensure schema present; stage_run for init_state recorded after intake (FK)
            init_res = run_init_state(db=db_path, state_root=paths.state_root, dry_run=False, emit_output=False)
            record("init_state", "ran", init_res)

        # Helper to read prior stage statuses (apply path only when DB exists)
        def prior(stage: str) -> str | None:
            if dry_run or not db_path.exists():
                return None
            conn = connect_db(db_path)
            try:
                return _stage_status(conn, event_id, stage)
            finally:
                conn.close()

        # --- intake ---
        intake_prior = prior("intake")
        if intake_prior and _should_skip_completed(intake_prior) and not dry_run:
            record(
                "intake",
                "skipped",
                {"stage": "intake", "status": intake_prior, "dry_run": False},
                reason="already_ok",
            )
            intake_status = intake_prior
        else:
            intake_res = run_intake(
                event_id,
                source=source,
                name=name,
                source_uri=source_uri,
                local_path=local_path,
                db=db_path,
                policies_path=pol_path,
                dry_run=dry_run,
                emit_output=False,
            )
            record("intake", "ran", intake_res)
            intake_status = intake_res.get("status")
            if intake_status == "error":
                blocked_reason = "intake_error"
                overall = "error"

            # After intake creates event, record init_state stage_run if apply
            if not dry_run and intake_status in OK_STATUSES and db_path.exists():
                conn = connect_db(db_path)
                try:
                    from _common import upsert_stage_run

                    # Ensure event exists (intake creates it)
                    if conn.execute(
                        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone():
                        upsert_stage_run(
                            conn, event_id, "init_state", "ok", summary="schema ready"
                        )
                        conn.commit()
                finally:
                    conn.close()

        # --- validate ---
        if blocked_reason:
            record("validate", "blocked", reason=blocked_reason)
            record("static_scan", "blocked", reason=blocked_reason)
        else:
            validate_prior = prior("validate")
            if validate_prior and validate_prior in BLOCKING_STATUSES and not dry_run:
                record(
                    "validate",
                    "skipped",
                    {"stage": "validate", "status": validate_prior},
                    reason="prior_blocking",
                )
                blocked_reason = f"validate:{validate_prior}"
                overall = "blocked"
                record("static_scan", "blocked", reason=blocked_reason)
            elif validate_prior and _should_skip_completed(validate_prior) and not dry_run:
                record(
                    "validate",
                    "skipped",
                    {"stage": "validate", "status": validate_prior},
                    reason="already_ok",
                )
            else:
                # Need candidate for validate — dry-run intake doesn't write, so dry-run
                # validate needs a temporary approach: if dry-run and local_path given,
                # we may still call run_validate which needs DB+candidate.
                # For dry-run without DB candidate: synthesize validate via validate_candidate
                # when local_path provided and DB missing/empty.
                if dry_run and (not db_path.exists() or prior("intake") is None):
                    # Simulate validate from local_path without DB
                    from validate import validate_candidate
                    from _common import get_limits, resolve_under_root, PROJECT_ROOT

                    limits = get_limits(policies)
                    if local_path:
                        try:
                            lp = resolve_under_root(PROJECT_ROOT, local_path)
                            validation = validate_candidate(lp, limits)
                            vstatus = "ok" if validation["ok"] else "rejected"
                            validate_res = {
                                "stage": "validate",
                                "status": vstatus,
                                "dry_run": True,
                                "message": "Validation complete (dry-run; synthetic, no DB)",
                                "event_id": event_id,
                                "summary": validation,
                                "action_required": not validation["ok"],
                            }
                        except ValueError as exc:
                            validate_res = {
                                "stage": "validate",
                                "status": "error",
                                "dry_run": True,
                                "message": str(exc),
                                "event_id": event_id,
                            }
                    else:
                        validate_res = {
                            "stage": "validate",
                            "status": "error",
                            "dry_run": True,
                            "message": "no local_path for dry-run validate",
                            "event_id": event_id,
                        }
                    record("validate", "ran", validate_res)
                else:
                    validate_res = run_validate(
                        event_id,
                        db=db_path,
                        policies_path=pol_path,
                        dry_run=dry_run,
                        emit_output=False,
                    )
                    record("validate", "ran", validate_res)

                vstat = stages_result["validate"]["result"].get("status")
                if vstat in ("rejected", "error"):
                    blocked_reason = f"validate:{vstat}"
                    overall = "blocked" if vstat == "rejected" else "error"
                    record("static_scan", "blocked", reason=blocked_reason)

        # --- static_scan ---
        if "static_scan" not in stages_result:
            scan_prior = prior("static_scan")
            if scan_prior and scan_prior in ("critical", "quarantined") and not dry_run:
                record(
                    "static_scan",
                    "skipped",
                    {"stage": "static_scan", "status": scan_prior},
                    reason="already_quarantined",
                )
                blocked_reason = f"static_scan:{scan_prior}"
                overall = "quarantined"
            elif scan_prior and _should_skip_completed(scan_prior) and not dry_run:
                record(
                    "static_scan",
                    "skipped",
                    {"stage": "static_scan", "status": scan_prior},
                    reason="already_ok",
                )
            else:
                if dry_run and (not db_path.exists() or stages_result.get("intake", {}).get("result", {}).get("status") == "ok"):
                    # Prefer local_path direct scan for dry-run without requiring DB candidate
                    from static_scan import scan_candidate
                    from _common import get_limits, resolve_under_root, PROJECT_ROOT

                    limits = get_limits(policies)
                    if local_path:
                        try:
                            lp = resolve_under_root(PROJECT_ROOT, local_path)
                            scan = scan_candidate(lp, limits)
                            if scan["has_critical"]:
                                sstatus = "critical"
                            elif scan["ok"]:
                                sstatus = "ok"
                            else:
                                sstatus = "flagged"
                            scan_res = {
                                "stage": "static_scan",
                                "status": sstatus,
                                "dry_run": True,
                                "message": "Static scan complete (dry-run; synthetic, no DB)",
                                "event_id": event_id,
                                "summary": scan,
                                "action_required": not scan["ok"],
                                "has_critical": scan["has_critical"],
                            }
                        except ValueError as exc:
                            scan_res = {
                                "stage": "static_scan",
                                "status": "error",
                                "dry_run": True,
                                "message": str(exc),
                                "event_id": event_id,
                            }
                    else:
                        scan_res = run_static_scan(
                            event_id,
                            db=db_path,
                            policies_path=pol_path,
                            state_root=paths.state_root,
                            dry_run=True,
                            emit_output=False,
                        )
                    record("static_scan", "ran", scan_res)
                else:
                    scan_res = run_static_scan(
                        event_id,
                        db=db_path,
                        policies_path=pol_path,
                        state_root=paths.state_root,
                        dry_run=dry_run,
                        emit_output=False,
                    )
                    record("static_scan", "ran", scan_res)

                sstat = stages_result["static_scan"]["result"].get("status")
                if sstat in ("critical", "quarantined"):
                    blocked_reason = f"static_scan:{sstat}"
                    overall = "quarantined"
                elif sstat == "flagged" and overall == "ok":
                    overall = "flagged"
                elif sstat == "error":
                    blocked_reason = "static_scan:error"
                    overall = "error"

        # If validate blocked static_scan, mark overall
        if stages_result.get("static_scan", {}).get("action") == "blocked" and overall == "ok":
            overall = "blocked"

        # --- report (always runs to summarize) ---
        report_res = run_report(
            db=db_path,
            dry_run=dry_run,
            event_id=event_id,
            pipeline_context={
                "blocked_reason": blocked_reason,
                "stages": {k: {"action": v["action"], "status": v.get("status")} for k, v in stages_result.items()},
            },
            emit_output=False,
        )
        record("report", "ran", report_res)

        out = {
            "stage": "pipeline",
            "status": overall,
            "dry_run": dry_run,
            "message": "Pipeline orchestration complete",
            "event_id": event_id,
            "item": name or event_id,
            "blocked_reason": blocked_reason,
            "stages": {
                k: {
                    "action": v["action"],
                    "status": v.get("status") or (v.get("result") or {}).get("status"),
                    "reason": v.get("reason"),
                    "summary": (v.get("result") or {}).get("summary"),
                    "message": (v.get("result") or {}).get("message"),
                    "has_critical": (v.get("result") or {}).get("has_critical"),
                }
                for k, v in stages_result.items()
            },
            "artifact_retention": retention,
            "ai_escalation": {
                "escalate_when": ai_esc.get("escalate_when"),
                "never_override": ai_esc.get("never_override"),
                "model_review_threshold": ai_esc.get("model_review_threshold"),
                "note": "Deterministic critical findings cannot be overridden by AI",
            },
            "timestamp": iso_now(),
            "project": "proskills",
        }
        if emit_output:
            emit_json(out)
        return out
    finally:
        lock_cm.__exit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestrate init_state→intake→validate→static_scan→report (dry-run default)."
    )
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--name", default=None)
    parser.add_argument("--source", default="manual")
    parser.add_argument("--source-uri", default=None)
    parser.add_argument("--local-path", default=None)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    add_state_root_arg(parser)
    parser.add_argument(
        "--locks-dir",
        type=Path,
        default=None,
        help="Override locks directory (default: <state-root>/locks)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Persist local state writes. Without this flag, dry-run=True.",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    result = run_pipeline(
        args.event_id,
        name=args.name,
        source=args.source,
        source_uri=args.source_uri,
        local_path=args.local_path,
        db=args.db,
        policies_path=args.policies,
        state_root=args.state_root,
        locks_dir=args.locks_dir,
        dry_run=dry_run,
        emit_output=True,
    )
    if result.get("status") == "locked":
        exit_fail(3)
    if result.get("status") == "error":
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
