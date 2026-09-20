#!/usr/bin/env python3
"""Copy staged catalog into website repo on a new branch and open a PR (never merge).

Dry-run by default: show diff stats only.
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import add_dry_run_apply_flags, emit, exit_fail, exit_ok, resolve_dry_run

DEFAULT_WEBSITE = Path("/workspace/website-ops")
DEFAULT_BRANCH_PREFIX = "operator/catalog-publish"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create website catalog PR from staged JSON.")
    add_dry_run_apply_flags(parser)
    parser.add_argument(
        "--website-repo",
        type=Path,
        default=DEFAULT_WEBSITE,
        help="Path to website git clone (default sibling website-ops)",
    )
    parser.add_argument(
        "--staged-catalog",
        type=Path,
        required=True,
        help="Path to skills-catalog.staged.json",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default=None,
        help="Branch name (default operator/catalog-publish-<timestamp>)",
    )
    parser.add_argument(
        "--title",
        type=str,
        default="operator: publish staged skills catalog",
        help="PR title",
    )
    return parser.parse_args()


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)


def _diff_stats(old: Path, new: Path) -> dict:
    try:
        old_data = json.loads(old.read_text(encoding="utf-8"))
        new_data = json.loads(new.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}
    old_ids = {s.get("id") for s in (old_data.get("skills") or [])}
    new_ids = {s.get("id") for s in (new_data.get("skills") or [])}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    return {
        "old_total": len(old_ids),
        "new_total": len(new_ids),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_ids_sample": added[:20],
        "removed_ids_sample": removed[:20],
        "bytes_old": old.stat().st_size if old.is_file() else 0,
        "bytes_new": new.stat().st_size if new.is_file() else 0,
    }


def main() -> None:
    args = _parse_args()
    dry_run = resolve_dry_run(args)

    website = args.website_repo.resolve()
    staged = args.staged_catalog.resolve()
    target = website / "public" / "skills-catalog.json"

    if not staged.is_file():
        emit(stage="create_pr", status="error", dry_run=dry_run, message=f"missing staged catalog: {staged}", never_merges=True)
        exit_fail(1)
    if not website.is_dir() or not (website / ".git").exists():
        emit(stage="create_pr", status="error", dry_run=dry_run, message=f"not a git repo: {website}", never_merges=True)
        exit_fail(1)

    stats = _diff_stats(target, staged) if target.is_file() else {"note": "no existing catalog"}

    from publish_lib import iso_now

    branch = args.branch or f"{DEFAULT_BRANCH_PREFIX}-{iso_now().replace(':', '').replace('-', '')[:15]}"

    if dry_run:
        emit(
            stage="create_pr",
            status="ok",
            dry_run=True,
            message="create_pr dry-run: would copy staged catalog, push branch, open PR (never merge)",
            never_merges=True,
            plan={
                "website_repo": str(website),
                "staged": str(staged),
                "target": str(target),
                "branch": branch,
                "title": args.title,
                "diff_stats": stats,
            },
        )
        exit_ok()

    # --apply: branch, copy, commit, push, gh pr create (never merge)
    status = _run(["git", "status", "--porcelain"], cwd=website)
    if status.returncode != 0:
        emit(stage="create_pr", status="error", dry_run=False, message=status.stderr, never_merges=True)
        exit_fail(1)

    co = _run(["git", "checkout", "-b", branch], cwd=website)
    if co.returncode != 0:
        # try switch if exists
        co2 = _run(["git", "checkout", branch], cwd=website)
        if co2.returncode != 0:
            emit(
                stage="create_pr",
                status="error",
                dry_run=False,
                message=co.stderr or co2.stderr,
                never_merges=True,
            )
            exit_fail(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(staged.read_text(encoding="utf-8"), encoding="utf-8")

    _run(["git", "add", "public/skills-catalog.json"], cwd=website)
    commit = _run(
        ["git", "commit", "-m", f"chore(catalog): stage operator publish ({branch})"],
        cwd=website,
    )
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        emit(stage="create_pr", status="error", dry_run=False, message=commit.stderr or commit.stdout, never_merges=True)
        exit_fail(1)

    push = _run(["git", "push", "-u", "origin", branch], cwd=website)
    if push.returncode != 0:
        emit(stage="create_pr", status="error", dry_run=False, message=push.stderr or push.stdout, never_merges=True)
        exit_fail(1)

    body = (
        "## Summary\n"
        "- Operator staged catalog append (no existing skill id/slug/repo overwritten).\n"
        "- Diff stats: "
        + json.dumps(stats)
        + "\n\n## Notes\n- Opened by create_pr.py; **do not auto-merge**.\n"
    )
    pr = _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            args.title,
            "--body",
            body,
            "--base",
            "main",
            "--head",
            branch,
        ],
        cwd=website,
    )
    if pr.returncode != 0:
        emit(
            stage="create_pr",
            status="error",
            dry_run=False,
            message=pr.stderr or pr.stdout,
            never_merges=True,
            pushed_branch=branch,
        )
        exit_fail(1)

    pr_url = (pr.stdout or "").strip().splitlines()[-1] if pr.stdout else ""
    emit(
        stage="create_pr",
        status="ok",
        dry_run=False,
        message=f"opened PR (never merges): {pr_url}",
        never_merges=True,
        pr_url=pr_url,
        branch=branch,
        diff_stats=stats,
    )
    exit_ok()


if __name__ == "__main__":
    main()
