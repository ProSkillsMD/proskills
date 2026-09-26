#!/usr/bin/env python3
"""Squash-merge a website catalog publish PR that has been stuck for > 90 minutes.

Committed replacement for the stuck-PR check the old /tmp intake scout carried, so the hourly
publisher can call it. Default is a DRY RUN (report only); pass --apply to merge.

A PR on ProSkillsMD/website is merged only when ALL of these hold:

* open, not draft, base branch `main`, head branch `operator/catalog-publish-*`
  (operator catalog publishes), authored by the operator account (default Asif2BD);
* never PR #29 or any number in --protected (default 29 and 1-15);
* created more than --min-age-min minutes ago (default 90);
* diff is exactly `public/skills-catalog.json` (catalog-only);
* mergeable == MERGEABLE and mergeStateStatus == CLEAN;
* checks passing: `gh pr checks` shows no failing/pending check. When the token cannot read checks
  (403 "Resource not accessible"), GitHub's CLEAN merge state is the evidence (CLEAN means no failing
  or pending required checks) and that is recorded as `checks_source: merge_state_clean`.

The merge is `gh pr merge --squash --match-head-commit <sha>` so a PR that changed since it was
inspected is never merged. Prints one JSON summary line; with --out also writes it to a file.
Exit code 0 unless listing PRs fails (2) or a merge attempt fails (1).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO = "ProSkillsMD/website"
CATALOG_FILES = ("public/skills-catalog.json",)
HEAD_PREFIX = "operator/catalog-publish-"
DEFAULT_PROTECTED = frozenset({29, *range(1, 16)})
DHAKA = timezone(timedelta(hours=6))

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_open_prs(run: Runner, repo: str = REPO) -> list[dict[str, Any]]:
    proc = run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json",
                "number,title,headRefName,baseRefName,headRefOid,createdAt,updatedAt,isDraft,"
                "mergeable,mergeStateStatus,author,url"])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh pr list failed").strip()[:300])
    return json.loads(proc.stdout or "[]")


def pr_files(run: Runner, number: int, repo: str = REPO) -> list[str] | None:
    proc = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", "files"])
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    return sorted(f.get("path") for f in data.get("files") or [] if f.get("path"))


def checks_state(run: Runner, number: int, repo: str = REPO) -> str:
    """'passing' | 'failing' | 'pending' | 'none' | 'unreadable'."""
    proc = run(["gh", "pr", "checks", str(number), "--repo", repo])
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    low = text.lower()
    if "resource not accessible" in low or "http 403" in low:
        return "unreadable"
    if "no checks reported" in low:
        return "none"
    states = [ln.split("\t")[1].strip().lower() for ln in (proc.stdout or "").splitlines() if ln.count("\t") >= 1]
    if any(s in ("fail", "failure", "cancel", "cancelled", "error", "timed_out", "action_required") for s in states):
        return "failing"
    if any(s in ("pending", "queued", "in_progress", "waiting") for s in states):
        return "pending"
    if proc.returncode == 0 and states:
        return "passing"
    if proc.returncode == 8:
        return "pending"
    return "failing" if proc.returncode == 1 and states else "unreadable"


def evaluate(pr: dict[str, Any], *, run: Runner, now: datetime, min_age_min: float, author: str | None,
             protected: frozenset[int], repo: str = REPO) -> dict[str, Any]:
    """Decision record for one open PR (no side effects besides read-only gh calls)."""
    n = int(pr.get("number") or 0)
    rec: dict[str, Any] = {"number": n, "url": pr.get("url"), "head": pr.get("headRefName"),
                           "head_sha": pr.get("headRefOid"), "mergeable": pr.get("mergeable"),
                           "merge_state": pr.get("mergeStateStatus"), "eligible": False}

    def skip(reason: str) -> dict[str, Any]:
        rec["skip_reason"] = reason
        return rec

    if n in protected:
        return skip("protected_pr")
    if not str(pr.get("headRefName") or "").startswith(HEAD_PREFIX):
        return skip("not_catalog_publish_branch")
    login = ((pr.get("author") or {}).get("login") or "") if isinstance(pr.get("author"), dict) else str(pr.get("author") or "")
    if author and login.lower() != author.lower():
        return skip("not_operator_author")
    if pr.get("isDraft"):
        return skip("draft")
    if (pr.get("baseRefName") or "main") != "main":
        return skip("base_not_main")
    created = _parse_ts(pr.get("createdAt"))
    if created is None:
        return skip("no_created_at")
    age = (now - created).total_seconds() / 60.0
    rec["age_min"] = round(age, 1)
    if age <= min_age_min:
        return skip("too_young")
    if pr.get("mergeable") != "MERGEABLE" or pr.get("mergeStateStatus") != "CLEAN":
        return skip("not_mergeable_clean")
    files = pr_files(run, n, repo)
    rec["files"] = files
    if files is None:
        return skip("files_unreadable")
    if tuple(files) != CATALOG_FILES:
        return skip("not_catalog_only")
    cs = checks_state(run, n, repo)
    rec["checks"] = cs
    if cs in ("failing", "pending"):
        return skip(f"checks_{cs}")
    rec["checks_source"] = "gh_pr_checks" if cs in ("passing", "none") else "merge_state_clean"
    if not pr.get("headRefOid"):
        return skip("no_head_sha")
    rec["eligible"] = True
    return rec


def squash_merge(run: Runner, number: int, head_sha: str, repo: str = REPO) -> dict[str, Any]:
    proc = run(["gh", "pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch",
                "--match-head-commit", head_sha])
    if proc.returncode != 0:
        return {"merged": False, "error": (proc.stderr or proc.stdout or "merge failed").strip()[:300]}
    view = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", "state,mergedAt,mergeCommit"])
    info = json.loads(view.stdout or "{}") if view.returncode == 0 else {}
    return {"merged": True, "state": info.get("state"), "merged_at": info.get("mergedAt"),
            "merge_sha": (info.get("mergeCommit") or {}).get("oid")}


def run_check(*, apply: bool, run: Runner = _default_runner, now: datetime | None = None,
              min_age_min: float = 90.0, author: str | None = "Asif2BD",
              protected: frozenset[int] = DEFAULT_PROTECTED, repo: str = REPO) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"checked_at_asia_dhaka": now.astimezone(DHAKA).isoformat(), "repo": repo,
                           "dry_run": not apply, "min_age_min": min_age_min, "prs": [], "merged": [],
                           "would_merge": [], "errors": []}
    try:
        prs = list_open_prs(run, repo)
    except Exception as e:  # listing failure: report, never act
        out["errors"].append(f"list_failed: {e}")
        out["status"] = "error"
        return out
    for pr in prs:
        rec = evaluate(pr, run=run, now=now, min_age_min=min_age_min, author=author, protected=protected, repo=repo)
        if rec["number"] in protected or rec.get("skip_reason") == "not_catalog_publish_branch":
            continue  # never report unrelated PRs in detail
        out["prs"].append(rec)
        if not rec["eligible"]:
            continue
        if not apply:
            out["would_merge"].append(rec["number"])
            continue
        res = squash_merge(run, rec["number"], rec["head_sha"], repo)
        rec["merge"] = res
        if res.get("merged"):
            out["merged"].append({"number": rec["number"], "merge_sha": res.get("merge_sha"),
                                  "merged_at": res.get("merged_at")})
        else:
            out["errors"].append(f"merge_failed #{rec['number']}: {res.get('error')}")
    out["status"] = "error" if out["errors"] else "ok"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="actually squash-merge (default: dry run)")
    ap.add_argument("--min-age-min", type=float, default=90.0)
    ap.add_argument("--author", default="Asif2BD", help="required PR author login ('' = any)")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--out", type=Path, default=None, help="also write the JSON summary here")
    args = ap.parse_args(argv)
    result = run_check(apply=args.apply, min_age_min=args.min_age_min, author=args.author or None, repo=args.repo)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    if any(e.startswith("list_failed") for e in result["errors"]):
        return 2
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
