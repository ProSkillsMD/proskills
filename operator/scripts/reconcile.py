#!/usr/bin/env python3
"""Compare live/local catalog vs staged/candidates; report missing/extra/drift.

Read-only. No writes. No AI usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import add_dry_run_apply_flags, emit, exit_fail, exit_ok, resolve_dry_run
from publish_lib import (
    build_catalog_identity_set,
    catalog_skill_identity,
    load_catalog,
    parse_github_source,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile catalog vs staged/candidates (read-only).")
    add_dry_run_apply_flags(parser)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Live or local skills-catalog.json (default: fetch live or sibling path)",
    )
    parser.add_argument(
        "--staged",
        type=Path,
        default=None,
        help="Optional skills-catalog.staged.json",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Optional discover candidates JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write reconcile report JSON (only with --apply)",
    )
    return parser.parse_args()


def _ids(catalog: dict[str, Any]) -> set[str]:
    return {str(s.get("id")) for s in (catalog.get("skills") or []) if s.get("id")}


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return list(data.get("candidates") or [])


def reconcile(
    catalog: dict[str, Any],
    *,
    staged: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    live_ids = _ids(catalog)
    live_identities = build_catalog_identity_set(catalog)
    report: dict[str, Any] = {
        "live_total": len(live_ids),
        "live_identities": len(live_identities),
    }

    if staged is not None:
        staged_ids = _ids(staged)
        report["staged_total"] = len(staged_ids)
        report["missing_from_live"] = sorted(staged_ids - live_ids)  # in staged, not live (= new)
        report["extra_in_live"] = sorted(live_ids - staged_ids)  # in live, dropped from staged
        # drift: same id, different repo_url / slug
        staged_by_id = {s.get("id"): s for s in (staged.get("skills") or []) if s.get("id")}
        drift = []
        for s in catalog.get("skills") or []:
            sid = s.get("id")
            if not sid or sid not in staged_by_id:
                continue
            other = staged_by_id[sid]
            changes = {}
            for field in ("slug", "repo_url", "skill_path", "name"):
                if s.get(field) != other.get(field):
                    changes[field] = {"live": s.get(field), "staged": other.get(field)}
            if changes:
                drift.append({"id": sid, "changes": changes})
        report["drift"] = drift

    if candidates is not None:
        already = []
        novel = []
        for c in candidates:
            ident = c.get("identity")
            if not ident:
                p = parse_github_source(c.get("repo_url"))
                ident = p["identity"] if p else None
            if not ident:
                continue
            if ident.lower() in {x.lower() for x in live_identities}:
                already.append(ident)
            else:
                novel.append(ident)
        report["candidates_already_published"] = already
        report["candidates_novel"] = novel

    return report


def main() -> None:
    args = _parse_args()
    dry_run = resolve_dry_run(args)

    catalog_path = args.catalog
    if catalog_path is None:
        sibling = Path("/workspace/website-ops/public/skills-catalog.json")
        try:
            catalog = load_catalog(None)
        except RuntimeError:
            if sibling.is_file():
                catalog = load_catalog(sibling)
                catalog_path = sibling
            else:
                emit(stage="reconcile", status="error", dry_run=dry_run, message="no catalog available")
                exit_fail(1)
    else:
        catalog = load_catalog(catalog_path)

    staged = load_catalog(args.staged) if args.staged else None
    candidates = _load_candidates(args.candidates) if args.candidates else None

    report = reconcile(catalog, staged=staged, candidates=candidates)
    report["catalog_path"] = str(catalog_path) if catalog_path else "live"

    # reconcile is read-only; --apply only optionally writes the report file
    if not dry_run and args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    emit(
        stage="reconcile",
        status="ok",
        dry_run=dry_run,
        message="reconcile complete (read-only)",
        report=report,
    )
    exit_ok()


if __name__ == "__main__":
    main()
