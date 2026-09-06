#!/usr/bin/env python3
"""License, manifest, and metadata validation (text/file inspection only).

Replaces old Curio role: Rocket (rules path); Grok only if rules are ambiguous.
Never executes candidate code.
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
    connect_db,
    emit,
    ensure_schema,
    exit_fail,
    exit_ok,
    get_limits,
    load_policies,
    resolve_under_root,
    upsert_stage_run,
)

REQUIRED_MARKERS = ("SKILL.md", "skill.md", "manifest.json", "skill.json")


def _count_files_and_bytes(root: Path, max_files: int, max_bytes: int) -> tuple[int, int, list[str]]:
    """Walk files under root; return (file_count, total_bytes, relative_paths)."""
    count = 0
    total = 0
    rels: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            # count symlink as an entry but do not follow for size aggregation beyond link itself
            continue
        if not path.is_file():
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
            continue
        count += 1
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        total += size
        try:
            rels.append(str(path.relative_to(root)))
        except ValueError:
            rels.append(str(path))
        if count > max_files or total > max_bytes:
            break
    return count, total, rels


def validate_candidate(local_path: Path, limits: dict) -> dict:
    """Validate metadata presence and size limits. Text/file inspection only."""
    max_files = int(limits.get("max_files_per_candidate", 200))
    max_bytes = int(limits.get("max_total_bytes_per_candidate", 5242880))

    result: dict = {
        "local_path": str(local_path),
        "ok": True,
        "errors": [],
        "warnings": [],
        "markers_found": [],
    }

    if not local_path.is_dir():
        result["ok"] = False
        result["errors"].append("local_path is not a directory")
        return result

    for marker in REQUIRED_MARKERS:
        if (local_path / marker).is_file():
            result["markers_found"].append(marker)

    if not result["markers_found"]:
        result["ok"] = False
        result["errors"].append(
            f"missing required marker file; need one of: {', '.join(REQUIRED_MARKERS)}"
        )

    count, total, _rels = _count_files_and_bytes(local_path, max_files, max_bytes)
    result["file_count"] = count
    result["total_bytes"] = total
    result["max_files"] = max_files
    result["max_bytes"] = max_bytes

    if count > max_files:
        result["ok"] = False
        result["errors"].append(f"file count {count} exceeds max_files_per_candidate {max_files}")
    if total > max_bytes:
        result["ok"] = False
        result["errors"].append(
            f"total bytes {total} exceeds max_total_bytes_per_candidate {max_bytes}"
        )

    return result


def run_validate(
    event_id: str,
    *,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Validate candidate for event_id. Returns status dict."""
    event_id = event_id.strip()
    db_path = Path(db)
    pol_path = Path(policies_path)

    if not event_id:
        result = {
            "stage": "validate",
            "status": "error",
            "dry_run": dry_run,
            "message": "empty event_id",
            "event_id": event_id,
        }
        if emit_output:
            emit(stage="validate", status="error", dry_run=dry_run, message="empty event_id")
        return result

    policies = load_policies(pol_path) if pol_path.exists() else {}
    limits = get_limits(policies)

    if not db_path.exists():
        result = {
            "stage": "validate",
            "status": "error",
            "dry_run": dry_run,
            "message": f"database not found: {db_path}",
            "event_id": event_id,
        }
        if emit_output:
            emit(
                stage="validate",
                status="error",
                dry_run=dry_run,
                message=result["message"],
                event_id=event_id,
            )
        return result

    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT event_id, name, local_path, status FROM candidates WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            result = {
                "stage": "validate",
                "status": "error",
                "dry_run": dry_run,
                "message": f"no candidate for event_id={event_id}",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="validate",
                    status="error",
                    dry_run=dry_run,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

        local_raw = row["local_path"]
        if not local_raw:
            result = {
                "stage": "validate",
                "status": "error",
                "dry_run": dry_run,
                "message": "candidate has no local_path",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="validate",
                    status="error",
                    dry_run=dry_run,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

        try:
            local_path = resolve_under_root(PROJECT_ROOT, local_raw)
        except ValueError as exc:
            result = {
                "stage": "validate",
                "status": "error",
                "dry_run": dry_run,
                "message": str(exc),
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="validate",
                    status="error",
                    dry_run=dry_run,
                    message=str(exc),
                    event_id=event_id,
                )
            return result

        if not local_path.exists():
            abs_try = Path(local_raw)
            if abs_try.is_absolute():
                try:
                    local_path = resolve_under_root(PROJECT_ROOT, abs_try)
                except ValueError as exc:
                    result = {
                        "stage": "validate",
                        "status": "error",
                        "dry_run": dry_run,
                        "message": str(exc),
                        "event_id": event_id,
                    }
                    if emit_output:
                        emit(
                            stage="validate",
                            status="error",
                            dry_run=dry_run,
                            message=str(exc),
                            event_id=event_id,
                        )
                    return result

        validation = validate_candidate(local_path, limits)
        status = "ok" if validation["ok"] else "rejected"
        summary = json.dumps(validation, ensure_ascii=False)

        if dry_run:
            result = {
                "stage": "validate",
                "status": status,
                "dry_run": True,
                "message": "Validation complete (dry-run; no DB write)",
                "event_id": event_id,
                "item": row["name"],
                "summary": validation,
                "action_required": not validation["ok"],
            }
            if emit_output:
                emit(
                    stage="validate",
                    status=status,
                    dry_run=True,
                    message=result["message"],
                    event_id=event_id,
                    item=row["name"],
                    summary=validation,
                    action_required=not validation["ok"],
                )
            return result

        upsert_stage_run(conn, event_id, "validate", status, summary=summary)
        if validation["ok"]:
            conn.execute(
                "UPDATE candidates SET status = ? WHERE event_id = ?",
                ("validated", event_id),
            )
        else:
            conn.execute(
                "UPDATE candidates SET status = ? WHERE event_id = ?",
                ("validate_failed", event_id),
            )
        conn.commit()
        result = {
            "stage": "validate",
            "status": status,
            "dry_run": False,
            "message": "Validation recorded",
            "event_id": event_id,
            "item": row["name"],
            "summary": validation,
            "action_required": not validation["ok"],
        }
        if emit_output:
            emit(
                stage="validate",
                status=status,
                dry_run=False,
                message=result["message"],
                event_id=event_id,
                item=row["name"],
                summary=validation,
                action_required=not validation["ok"],
            )
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate license/manifest/metadata (Rocket rules)."
    )
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    from _common import add_dry_run_apply_flags, resolve_dry_run

    add_dry_run_apply_flags(parser)
    args = parser.parse_args()

    result = run_validate(
        args.event_id,
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
