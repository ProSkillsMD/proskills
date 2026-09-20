#!/usr/bin/env python3
"""Orchestrate a canary publish plan: discover subset → catalog_update → checklist.

Does not open website PRs by default (create_pr is a separate --apply step later).
Dry-run by default for nested steps unless --apply.
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PROJECT_ROOT, add_dry_run_apply_flags, emit, exit_fail, exit_ok, resolve_dry_run

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_CATALOG = Path("/workspace/website-ops/public/skills-catalog.json")
DEFAULT_CANDIDATES = PROJECT_ROOT / "state" / "artifacts" / "discover-candidates.json"
DEFAULT_STAGED = PROJECT_ROOT / "state" / "artifacts" / "skills-catalog.staged.json"
DEFAULT_DELTA = PROJECT_ROOT / "state" / "artifacts" / "delta.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish canary orchestrator.")
    add_dry_run_apply_flags(parser)
    parser.add_argument(
        "--issues",
        type=str,
        default=None,
        help="Comma-separated issue numbers to canary (e.g. 1,2,3)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="Auto-pick top N from discover output when --issues omitted (default 3)",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_CANDIDATES,
        help="Discover candidates JSON (run discover first or pass --discover)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        default=False,
        help="Run discover.py --apply first to refresh candidates",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--staged-out", type=Path, default=DEFAULT_STAGED)
    parser.add_argument("--delta-out", type=Path, default=DEFAULT_DELTA)
    parser.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Pass --offline to catalog_update (no network SKILL.md fetch)",
    )
    return parser.parse_args()


def _run_script(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _checklist_urls(skills: list[dict[str, Any]]) -> list[str]:
    urls = []
    for s in skills:
        cat = s.get("category") or "other"
        slug = s.get("slug") or s.get("id")
        if slug:
            urls.append(f"https://proskills.md/skills/{cat}/{slug}")
    return urls


def main() -> None:
    args = _parse_args()
    dry_run = resolve_dry_run(args)

    if args.discover:
        disc_cmd = [
            str(SCRIPTS / "discover.py"),
            "--catalog",
            str(args.catalog),
            "--output",
            str(args.candidates),
            "--limit",
            "25",
            "--from-issues",
        ]
        if not dry_run:
            disc_cmd.append("--apply")
        proc = _run_script(disc_cmd)
        print(proc.stdout)
        if proc.returncode != 0:
            emit(stage="publish_canary", status="error", dry_run=dry_run, message=proc.stderr or proc.stdout)
            exit_fail(1)

    if not args.candidates.is_file() and dry_run:
        # allow dry-run plan without candidates file
        emit(
            stage="publish_canary",
            status="ok",
            dry_run=True,
            message="publish_canary dry-run: no candidates file yet; run discover --apply or pass --candidates",
            plan={"next": "python3 operator/scripts/discover.py --catalog <path> --apply"},
        )
        exit_ok()

    if not args.candidates.is_file():
        emit(stage="publish_canary", status="error", dry_run=dry_run, message=f"missing candidates: {args.candidates}")
        exit_fail(1)

    data = json.loads(args.candidates.read_text(encoding="utf-8"))
    all_cands = data if isinstance(data, list) else list(data.get("candidates") or [])

    if args.issues:
        wanted = {int(x.strip()) for x in args.issues.split(",") if x.strip()}
        selected = [c for c in all_cands if c.get("issue") in wanted]
    else:
        selected = all_cands[: args.top]

    if not selected:
        emit(stage="publish_canary", status="error", dry_run=dry_run, message="no candidates selected")
        exit_fail(1)

    # Write filtered candidates temp
    filtered_path = args.candidates.parent / "canary-candidates.json"
    filtered_payload = {"candidates": selected, "canary": True}
    if not dry_run:
        filtered_path.parent.mkdir(parents=True, exist_ok=True)
        filtered_path.write_text(json.dumps(filtered_payload, indent=2) + "\n", encoding="utf-8")
    else:
        # still write to a temp under /tmp for nested dry-run of catalog_update
        filtered_path = Path("/tmp/proskills-canary-candidates.json")
        filtered_path.write_text(json.dumps(filtered_payload, indent=2) + "\n", encoding="utf-8")

    # Always run catalog_update dry-run first for visibility
    base_cu = [
        str(SCRIPTS / "catalog_update.py"),
        "--candidates",
        str(filtered_path),
        "--catalog",
        str(args.catalog),
        "--out",
        str(args.staged_out),
        "--delta-out",
        str(args.delta_out),
    ]
    if args.offline:
        base_cu.append("--offline")

    dry_proc = _run_script(base_cu)  # no --apply
    print(dry_proc.stdout)
    if dry_proc.returncode != 0:
        emit(stage="publish_canary", status="error", dry_run=dry_run, message=dry_proc.stderr or dry_proc.stdout)
        exit_fail(1)

    if dry_run:
        emit(
            stage="publish_canary",
            status="ok",
            dry_run=True,
            message=f"canary dry-run for {len(selected)} candidates; re-run with --apply to write staged catalog",
            selected=[{"issue": c.get("issue"), "identity": c.get("identity")} for c in selected],
        )
        exit_ok()

    apply_proc = _run_script(base_cu + ["--apply"])
    print(apply_proc.stdout)
    if apply_proc.returncode != 0:
        emit(stage="publish_canary", status="error", dry_run=False, message=apply_proc.stderr or apply_proc.stdout)
        exit_fail(1)

    checklist: list[str] = []
    if args.delta_out.is_file():
        delta = json.loads(args.delta_out.read_text(encoding="utf-8"))
        checklist = _checklist_urls(delta.get("added") or [])

    emit(
        stage="publish_canary",
        status="ok",
        dry_run=False,
        message="canary staged; verify checklist URLs after website PR + deploy",
        selected=[{"issue": c.get("issue"), "identity": c.get("identity")} for c in selected],
        staged=str(args.staged_out),
        delta=str(args.delta_out),
        verification_checklist=checklist,
        next_step="python3 operator/scripts/create_pr.py --staged-catalog <staged> --apply  # when ready; never merges",
    )
    exit_ok()


if __name__ == "__main__":
    main()
