#!/usr/bin/env python3
"""Local operator health report: DB counts + optional saved inventory JSON.

Dry-run by default (prints summary only). --apply writes a redacted JSON
report under <state-root>/logs/. Never calls the network. Optional inventory
counts come only from a local --inventory-json file.
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
    DEFAULT_STATE_ROOT,
    StateRootError,
    add_dry_run_apply_flags,
    add_state_root_arg,
    connect_db,
    emit_json,
    ensure_state_dirs,
    exit_fail,
    exit_ok,
    get_state_paths,
    iso_now,
    redact_secrets,
    resolve_dry_run,
    write_file_owner_only,
)


def _db_counts(db_path: Path) -> dict[str, Any]:
    empty = {
        "db_exists": False,
        "events": 0,
        "candidates_total": 0,
        "candidates_by_status": {},
        "stage_runs_total": 0,
        "stage_runs_by_stage_status": {},
        "artifacts": 0,
        "pending_approvals": 0,
        "outbound_events": 0,
        "outbound_by_status": {},
    }
    if not db_path.exists():
        return empty
    conn = connect_db(db_path)
    try:

        def scalar(sql: str, *args: Any) -> int:
            row = conn.execute(sql, args).fetchone()
            return int(row[0]) if row else 0

        return {
            "db_exists": True,
            "events": scalar("SELECT COUNT(*) FROM events"),
            "candidates_total": scalar("SELECT COUNT(*) FROM candidates"),
            "candidates_by_status": {
                r["status"]: r["c"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS c FROM candidates GROUP BY status"
                )
            },
            "stage_runs_total": scalar("SELECT COUNT(*) FROM stage_runs"),
            "stage_runs_by_stage_status": {
                f"{r['stage']}:{r['status']}": r["c"]
                for r in conn.execute(
                    "SELECT stage, status, COUNT(*) AS c "
                    "FROM stage_runs GROUP BY stage, status"
                )
            },
            "artifacts": scalar("SELECT COUNT(*) FROM artifacts"),
            "pending_approvals": scalar(
                "SELECT COUNT(*) FROM approvals "
                "WHERE status IN ('pending', 'requested')"
            ),
            "outbound_events": scalar("SELECT COUNT(*) FROM outbound_events"),
            "outbound_by_status": {
                (r["status"] or "unknown"): r["c"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS c "
                    "FROM outbound_events GROUP BY status"
                )
            },
        }
    finally:
        conn.close()


def _load_inventory(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.is_file():
        return {"error": f"inventory file missing: {path}", "path": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": redact_secrets(str(exc)), "path": str(path)}
    if not isinstance(raw, dict):
        return {"error": "inventory root must be object", "path": str(path)}
    # Prefer a compact summary block if present; never pass through secrets.
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    org = raw.get("org") if isinstance(raw.get("org"), dict) else {}
    repos = raw.get("repos") if isinstance(raw.get("repos"), list) else []
    safe_repos = []
    for r in repos:
        if not isinstance(r, dict):
            continue
        safe_repos.append(
            {
                "name": redact_secrets(str(r.get("name") or "")),
                "full_name": redact_secrets(str(r.get("full_name") or "")),
                "open_issues_approx": r.get("open_issues_approx"),
                "has_workflows_dir": r.get("has_workflows_dir"),
                "notes": redact_secrets(str(r.get("notes") or ""))[:500],
            }
        )
    return {
        "path": str(path),
        "generated_at": redact_secrets(str(raw.get("generated_at") or "")),
        "org_login": redact_secrets(str(org.get("login") or raw.get("org_login") or "")),
        "summary": {k: summary[k] for k in list(summary)[:40]},
        "repo_count": len(safe_repos),
        "repos": safe_repos,
        "labels_curio": raw.get("labels_curio")
        if isinstance(raw.get("labels_curio"), list)
        else None,
    }


def run_health_report(
    *,
    db: Path | str = DEFAULT_DB_PATH,
    state_root: Path | str | None = None,
    inventory_json: Path | str | None = None,
    include_github: bool = False,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Build health report. Network is never used (include_github ignored/false)."""
    del include_github  # explicit: health_report never calls GitHub
    db_path = Path(db)
    inv_path = Path(inventory_json) if inventory_json else None
    try:
        paths = get_state_paths(
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )
    except StateRootError as exc:
        result = {
            "stage": "health_report",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    counts = _db_counts(db_path)
    inventory = _load_inventory(inv_path)
    report: dict[str, Any] = {
        "stage": "health_report",
        "status": "ok",
        "dry_run": dry_run,
        "timestamp": iso_now(),
        "state_root": str(paths.state_root),
        "db": str(db_path),
        "db_counts": counts,
        "inventory": inventory,
        "network": {
            "include_github": False,
            "called": False,
            "note": "health_report never performs network calls; use inventory JSON",
        },
        "message": "health report generated (dry-run)" if dry_run else "health report written",
    }

    # Redact any secret-like strings in serialized form
    redacted_text = redact_secrets(json.dumps(report, ensure_ascii=False, indent=2))
    report = json.loads(redacted_text)

    if not dry_run:
        ensure_state_dirs(paths)
        out_name = f"health_report_{iso_now().replace(':', '').replace('-', '')}.json"
        # Keep filename filesystem-safe
        out_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in out_name)
        out_path = paths.logs_dir / out_name
        write_file_owner_only(
            out_path,
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            state_root=paths.state_root,
        )
        report["report_path"] = str(out_path)
        report["message"] = f"health report written to {out_path.name}"

    if emit_output:
        emit_json(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Local DB + optional inventory health report (dry-run default; "
            "no network)."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    add_state_root_arg(parser)
    parser.add_argument(
        "--inventory-json",
        type=Path,
        default=None,
        help="Optional saved inventory JSON (e.g. inventory/inventory-2026-09-06.json)",
    )
    parser.add_argument(
        "--include-github",
        action="store_true",
        default=False,
        help="Ignored: health_report never calls GitHub (kept for CLI clarity).",
    )
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    dry_run = resolve_dry_run(args)
    result = run_health_report(
        db=args.db,
        state_root=args.state_root,
        inventory_json=args.inventory_json,
        include_github=bool(args.include_github),
        dry_run=dry_run,
        emit_output=True,
    )
    if result.get("status") == "error":
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
