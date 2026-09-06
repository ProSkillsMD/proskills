#!/usr/bin/env python3
"""Purge structured evidence older than artifact_retention.structured_evidence_days.

Dry-run by default; --apply deletes. Never inspects or prints candidate secret values.
Only deletes paths/meta already stored (meta should already be redacted).
Cleanup operates only inside resolved state-root and never follows escaping symlinks.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    DEFAULT_STATE_ROOT,
    StateRootError,
    add_dry_run_apply_flags,
    add_state_root_arg,
    connect_db,
    emit_json,
    ensure_schema,
    exit_fail,
    exit_ok,
    get_artifact_retention,
    get_state_paths,
    iso_now,
    iter_files_no_follow,
    load_policies,
    redact_secrets,
    resolve_dry_run,
    resolve_state_root,
    safe_unlink_under_root,
)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_log_path(path: str) -> str:
    """Redact any secret-like content from a path/meta snippet before logging."""
    return redact_secrets(path)[:500]


def run_purge(
    *,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    state_root: Path | str | None = None,
    artifacts_dir: Path | str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Purge structured evidence files and DB rows older than retention.

    Artifacts directory is derived from --state-root (not from db.parent).
    Optional artifacts_dir override must resolve inside state-root.
    """
    db_path = Path(db)
    pol_path = Path(policies_path)
    try:
        root = resolve_state_root(state_root if state_root is not None else DEFAULT_STATE_ROOT)
        paths = get_state_paths(root)
    except StateRootError as exc:
        result = {
            "stage": "purge",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "timestamp": iso_now(),
            "project": "proskills",
        }
        if emit_output:
            emit_json(result)
        return result

    if artifacts_dir is not None:
        art_dir = Path(artifacts_dir)
        # Must stay inside state-root
        try:
            art_real = Path(os.path.realpath(str(art_dir if art_dir.exists() else art_dir)))
            # If not yet created, ensure intended path under root
            if art_dir.exists() or art_dir.is_symlink():
                if art_dir.is_symlink():
                    raise StateRootError(f"symlinked artifacts dir rejected: {art_dir}")
                art_real.relative_to(root)
            else:
                # parent must be under root
                parent_real = Path(os.path.realpath(str(art_dir.parent)))
                parent_real.relative_to(root)
            art_dir = art_dir if not art_dir.exists() else Path(os.path.realpath(str(art_dir)))
        except (ValueError, StateRootError, OSError) as exc:
            result = {
                "stage": "purge",
                "status": "error",
                "dry_run": dry_run,
                "message": f"artifacts_dir must be under state-root: {exc}",
                "timestamp": iso_now(),
                "project": "proskills",
            }
            if emit_output:
                emit_json(result)
            return result
    else:
        art_dir = paths.artifacts_dir

    policies = load_policies(pol_path) if pol_path.exists() else {}
    retention = get_artifact_retention(policies)
    days = int(retention.get("structured_evidence_days", 30))
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    would_delete_files: list[str] = []
    would_delete_rows: list[int] = []
    deleted_files: list[str] = []
    deleted_rows: list[int] = []

    # File-system purge under state-root artifacts (no symlink follow out)
    if art_dir.is_dir() and not art_dir.is_symlink():
        for path in sorted(iter_files_no_follow(art_dir)):
            try:
                # Use lstat for mtime; do not follow
                st = path.lstat()
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                rel = str(path)
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    try:
                        rel = str(path.relative_to(art_dir))
                    except ValueError:
                        pass
                safe = _safe_log_path(rel)
                would_delete_files.append(safe)
                if not dry_run:
                    if safe_unlink_under_root(path, root):
                        deleted_files.append(safe)

    # DB artifact rows
    if db_path.exists():
        conn = connect_db(db_path)
        try:
            ensure_schema(conn)
            rows = conn.execute(
                "SELECT id, path, meta_json, created_at FROM artifacts"
            ).fetchall()
            for row in rows:
                created = _parse_iso(row["created_at"])
                if created is None or created >= cutoff:
                    continue
                rid = int(row["id"])
                would_delete_rows.append(rid)
                fpath = row["path"]
                if fpath and not dry_run:
                    fp = Path(fpath)
                    if not fp.is_absolute():
                        # Paths are relative to state-root (preferred) or legacy project
                        cand = root / fp
                        if not cand.exists():
                            cand = Path(fpath)
                            if not cand.is_absolute():
                                from _common import PROJECT_ROOT

                                cand = PROJECT_ROOT / fp
                        fp = cand
                    try:
                        if safe_unlink_under_root(fp, root):
                            safe = _safe_log_path(str(fpath))
                            if safe not in deleted_files:
                                deleted_files.append(safe)
                    except (OSError, ValueError):
                        pass
                if not dry_run:
                    conn.execute("DELETE FROM artifacts WHERE id = ?", (rid,))
                    deleted_rows.append(rid)
            if not dry_run:
                conn.commit()
        finally:
            conn.close()

    result = {
        "stage": "purge",
        "status": "ok",
        "dry_run": dry_run,
        "message": (
            "Would purge structured evidence older than retention"
            if dry_run
            else "Purged structured evidence older than retention"
        ),
        "state_root": str(root),
        "retention_days": days,
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "would_delete_files": would_delete_files if dry_run else [],
        "would_delete_rows": would_delete_rows if dry_run else [],
        "deleted_files": deleted_files if not dry_run else [],
        "deleted_rows": deleted_rows if not dry_run else [],
        "file_count": len(would_delete_files if dry_run else deleted_files),
        "row_count": len(would_delete_rows if dry_run else deleted_rows),
        "timestamp": iso_now(),
        "project": "proskills",
    }
    if emit_output:
        emit_json(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge structured evidence older than retention (dry-run default)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    add_state_root_arg(parser)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Override artifacts directory (must remain under --state-root)",
    )
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    result = run_purge(
        db=args.db,
        policies_path=args.policies,
        state_root=args.state_root,
        artifacts_dir=args.artifacts_dir,
        dry_run=resolve_dry_run(args),
        emit_output=True,
    )
    if result.get("status") == "error":
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
