#!/usr/bin/env python3
"""Refresh live catalog skills with the full skill-folder ``files`` array.

One skill at a time, highest-traffic first. A ranking file (CSV ``slug,views``
or JSON) sets the order; skills that are not listed follow, by GitHub stars
then slug. Dry-run is the default: it prints that order and, when
``--preview-slug`` is given, the catalog JSON byte delta for those skills.

``--apply`` writes a website PR that touches only ``public/skills-catalog.json``
on branch ``operator/catalog-publish-*``. Batches are small (default 10 skills
inspected per run). The daily ceiling is the publisher's Asia/Dhaka cap
(100), counted the same way as ``publish_run.py`` (``added_count`` on merged
``operator/catalog-publish-*`` PRs). This command does not merge.

Progress is stored in ``operator/state/skill-files-backfill.json``. A killed
run continues at the next skill. A finished pass starts over and is a no-op
when upstream blob SHAs and SKILL.md have not changed.

Content policy (adult/NSFW, piracy, illegal) still applies. A skill whose
upstream text now trips it is flagged for review and left unchanged.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PROJECT_ROOT, emit, exit_fail, exit_ok  # noqa: E402
from publish_lib import catalog_json_dumps  # noqa: E402
from publish_run import (  # noqa: E402
    CATALOG_REL,
    DAILY_CAP,
    HEAD_PREFIX,
    WEBSITE_REPO,
    added_count,
    now_dhaka,
    to_dhaka_date,
)
from skill_files import (  # noqa: E402
    BackfillState,
    load_ranking,
    make_client,
    run_backfill,
)

DEFAULT_STATE = PROJECT_ROOT / "state" / "skill-files-backfill.json"
DEFAULT_BATCH = 10
Runner = Callable[[Sequence[str], Path | None], "subprocess.CompletedProcess[str]"]


def _run(cmd: Sequence[str], cwd: Path | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False, timeout=180)


def budget_remaining(merged_prs: list[dict[str, Any]], *, cap: int, today: str) -> int:
    """Skills already counted today against the publisher's daily cap."""
    used = 0
    for pr in merged_prs:
        if to_dhaka_date(pr.get("mergedAt")) == today:
            used += added_count(pr)
    return max(0, int(cap) - used)


def open_publish_heads(prs: list[dict[str, Any]]) -> list[str]:
    heads = []
    for pr in prs:
        head = str(pr.get("headRefName") or "")
        if head.startswith(HEAD_PREFIX):
            heads.append(head)
    return heads


def catalog_pr_body(slugs: list[str], *, old_total: int, new_total: int, diff_bytes: int) -> str:
    """PR body the existing publisher already understands.

    ``added_count`` is the number of skills whose files changed, so the daily
    cap counts a backfill the same way it counts a publish. The publish marker
    has no issue ids, so reconcile will not close candidate issues.
    """
    stats = {
        "old_total": old_total,
        "new_total": new_total,
        "added_count": len(slugs),
        "removed_count": 0,
        "updated_slugs_sample": slugs[:20],
        "diff_bytes": diff_bytes,
    }
    return (
        "## Summary\n"
        "- Backfill per-skill `files` from the skill folder (path, size, git blob sha, "
        "inline text within the caps in operator/docs/skill-files.md).\n"
        "- Existing id, slug, repo_url, author, and license are unchanged.\n"
        f"- Diff stats: {json.dumps(stats, sort_keys=True)}\n\n"
        "<!-- proskills:publish v1 items=[] -->\n"
        "<!-- proskills:backfill v1 slugs=" + json.dumps(slugs) + " -->\n"
    )


def catalog_pr_title(stamp: str, updated: int, total: int) -> str:
    return f"operator: catalog publish files-{stamp} (+{updated} skills → {total})"


def push_catalog_pr(
    run: Runner,
    website: Path,
    catalog: dict[str, Any],
    *,
    branch: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Commit only the catalog file and open a PR. Does not merge."""
    rel = CATALOG_REL
    status = run(["git", "status", "--porcelain"], website)
    if (status.stdout or "").strip():
        return {"ok": False, "reason": "dirty"}
    if not branch.startswith(HEAD_PREFIX):
        return {"ok": False, "reason": "bad_branch"}
    checked = run(["git", "checkout", "-q", "-b", branch], website)
    if checked.returncode != 0:
        return {"ok": False, "reason": "checkout_failed"}
    target = website / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(catalog_json_dumps(catalog) + "\n", encoding="utf-8")
    run(["git", "add", rel], website)
    diff = run(["git", "diff", "--cached", "--name-only"], website)
    names = [line.strip() for line in (diff.stdout or "").splitlines() if line.strip()]
    if names != [rel]:
        return {"ok": False, "reason": "not_catalog_only", "files": names}
    committed = run(["git", "commit", "-q", "-m", title], website)
    if committed.returncode != 0:
        return {"ok": False, "reason": "commit_failed"}
    pushed = run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], website)
    if pushed.returncode != 0:
        return {"ok": False, "reason": "push_failed"}
    created = run(
        ["gh", "pr", "create", "-R", WEBSITE_REPO, "--base", "main", "--head", branch, "--title", title, "--body", body],
        website,
    )
    return {
        "ok": created.returncode == 0,
        "files": names,
        "branch": branch,
        "merge_invoked": False,
        "stdout": (created.stdout or "")[-400:],
        "stderr": (created.stderr or "")[-400:],
    }


def _gh_json(run: Runner, args: list[str]) -> Any:
    proc = run(["gh", *args], None)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh failed")[:300])
    return json.loads(proc.stdout or "null")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill skill-folder files onto live catalog skills.")
    parser.add_argument("--catalog", type=Path, required=True, help="skills-catalog.json to read")
    parser.add_argument("--ranking", type=Path, default=None, help="CSV slug,views or JSON ranking")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="resume file under operator/state")
    parser.add_argument("--apply", action="store_true", help="refresh skills and open a catalog PR")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="skills inspected per run (small batch)")
    parser.add_argument("--cap", type=int, default=DAILY_CAP, help="daily update ceiling (publisher cap)")
    parser.add_argument("--preview-slug", action="append", default=[], help="fetch this slug during dry-run")
    parser.add_argument("--show-content", action="store_true", help="include inline file text in the dry-run JSON")
    parser.add_argument("--website", type=Path, default=None, help="website checkout, required with --apply")
    parser.add_argument("--published-today", type=int, default=None, help="override the merged-PR daily count")
    parser.add_argument("--stamp", default=None, help="branch timestamp override (tests)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, run: Runner = _run, client: Any = None) -> int:
    args = _parse_args(argv)
    dry_run = not args.apply
    if args.cap > DAILY_CAP:
        emit(stage="skill_files_backfill", status="error", dry_run=dry_run,
             message=f"--cap may not exceed {DAILY_CAP}")
        exit_fail(1)
    if args.batch < 1:
        emit(stage="skill_files_backfill", status="error", dry_run=dry_run, message="--batch must be >= 1")
        exit_fail(1)
    if args.apply and args.website is None:
        emit(stage="skill_files_backfill", status="error", dry_run=False, message="--apply requires --website")
        exit_fail(1)
    if not args.catalog.is_file():
        emit(stage="skill_files_backfill", status="error", dry_run=dry_run, message=f"missing catalog: {args.catalog}")
        exit_fail(1)

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    ranking = load_ranking(args.ranking) if args.ranking else {}
    state = BackfillState(args.state)
    state.load()

    today = now_dhaka().date().isoformat()
    budget_source = "assumed_full"
    open_heads: list[str] = []
    if args.published_today is not None:
        used = int(args.published_today)
        budget_source = "flag"
    elif args.apply:
        merged = _gh_json(run, ["pr", "list", "-R", WEBSITE_REPO, "--state", "merged", "--limit", "100",
                                "--json", "number,title,body,mergedAt,headRefName"])
        merged = [pr for pr in merged or [] if str(pr.get("headRefName") or "").startswith(HEAD_PREFIX)]
        used = sum(added_count(pr) for pr in merged if to_dhaka_date(pr.get("mergedAt")) == today)
        budget_source = "merged_prs"
        opened = _gh_json(run, ["pr", "list", "-R", WEBSITE_REPO, "--state", "open", "--limit", "100",
                                "--json", "number,headRefName"])
        open_heads = open_publish_heads(opened or [])
    else:
        used = 0
    remaining = max(0, int(args.cap) - used)
    if args.apply and open_heads:
        emit(stage="skill_files_backfill", status="ok", dry_run=False,
             message="open publish PR(s) pending; not starting another batch",
             open_prs=open_heads, remaining_today=remaining)
        exit_ok()

    gh_client = client
    if gh_client is None and (args.apply or args.preview_slug):
        gh_client = make_client()

    result = run_backfill(
        catalog, ranking, state=state, apply=args.apply, batch=args.batch, remaining=remaining,
        preview_slugs=list(args.preview_slug or []), client=gh_client, show_content=args.show_content,
    )
    pr: dict[str, Any] | None = None
    if args.apply and result["updated"]:
        stamp = args.stamp or datetime.now(now_dhaka().tzinfo).strftime("%Y%m%d-%H%M%S")
        branch = f"{HEAD_PREFIX}{stamp}"
        old_total = len(catalog.get("skills") or [])
        new_total = len(result["catalog"].get("skills") or [])
        title = catalog_pr_title(stamp, len(result["updated"]), new_total)
        body = catalog_pr_body(result["updated"], old_total=old_total, new_total=new_total, diff_bytes=result["diff_bytes"])
        pr = push_catalog_pr(run, args.website, result["catalog"], branch=branch, title=title, body=body)
        if pr.get("ok"):
            state.clear_pending()
        if not pr.get("ok"):
            emit(stage="skill_files_backfill", status="error", dry_run=False,
                 message=f"catalog PR was not opened: {pr.get('reason')}", pr=pr, order=[row["slug"] for row in result["order"]])
            exit_fail(1)
    elif args.apply and state is not None:
        # Inspected skills were recorded even when none changed. Nothing to push.
        pr = {"ok": True, "opened": False}

    emit(
        stage="skill_files_backfill",
        status="ok",
        dry_run=dry_run,
        message=(
            f"{'would update' if dry_run else 'updated'} {len(result['updated'])} skills; "
            f"diff_bytes={result['diff_bytes']}"
        ),
        order=[{"slug": row["slug"], "tier": row["tier"], "views": row["views"], "github_stars": row["github_stars"]}
               for row in result["order"]],
        planned_this_run=result["planned_this_run"],
        cursor=result["cursor"],
        diff_bytes=result["diff_bytes"],
        updated=result["updated"],
        unchanged=result["unchanged"],
        flagged=result["flagged"],
        stopped=result["stopped"],
        preview=result["results"],
        batch=args.batch,
        remaining_today=remaining,
        budget_source=budget_source,
        daily_cap=args.cap,
        files_touched=[CATALOG_REL],
        pr=pr,
    )
    exit_ok()
    return 0


if __name__ == "__main__":
    sys.exit(main())
