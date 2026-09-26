#!/usr/bin/env python3
"""Persist the operator's small state files on a dedicated `operator-state` branch (GitHub Actions).

Actions runners are ephemeral. Pure caches (repo/scan/skill/http caches) go through actions/cache and may be
lost at any time. The few files that carry real state (issue index with the daily filing counters, review
state with not-found first sightings, scout/source cursors, star readings) are committed to the orphan
branch `operator-state`, never to main.

  restore  copy <branch>:state/<rel> -> operator/state/<rel> for every whitelisted file present
  save     commit the whitelisted files to <branch> (created as an orphan branch when missing) and push

Uses the checkout's own git remote/credentials (actions/checkout with the GitHub App token), via a
temporary `git worktree`, so no token is ever handled here. Refuses any branch named main/master.
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

OPS = Path(__file__).resolve().parents[2]
STATE_ROOT = OPS / "operator" / "state"
DEFAULT_BRANCH = "operator-state"
FORBIDDEN_BRANCHES = frozenset({"main", "master", "HEAD"})
# relative to operator/state; small files only (caches go to actions/cache)
STATE_FILES = (
    "scout/cursor.json",
    "scout/issue-index.json",
    "scout/review-state.json",
    "sources/cursor.json",
    "sources/star-readings.json",
)
MAX_FILE_BYTES = 5 * 1024 * 1024

Runner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def _run(cmd: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), cwd=str(cwd), capture_output=True, text=True, check=False)


def _check_branch(branch: str) -> None:
    if not branch or branch in FORBIDDEN_BRANCHES or branch.startswith("refs/"):
        raise SystemExit(f"refusing state branch {branch!r}")


def restore(repo: Path, branch: str = DEFAULT_BRANCH, *, state_root: Path = STATE_ROOT,
            run: Runner = _run) -> dict:
    _check_branch(branch)
    fetched = run(["git", "fetch", "--depth", "1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], repo)
    if fetched.returncode != 0:
        return {"action": "restore", "branch": branch, "restored": [], "note": "state branch not found (first run)"}
    restored = []
    for rel in STATE_FILES:
        shown = run(["git", "show", f"origin/{branch}:state/{rel}"], repo)
        if shown.returncode != 0:
            continue
        try:
            json.loads(shown.stdout)
        except ValueError:
            continue  # never restore a corrupt file
        dest = state_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(shown.stdout, encoding="utf-8")
        restored.append(rel)
    return {"action": "restore", "branch": branch, "restored": restored}


def save(repo: Path, branch: str = DEFAULT_BRANCH, *, state_root: Path = STATE_ROOT, message: str = "",
         run: Runner = _run, apply: bool = True) -> dict:
    _check_branch(branch)
    files = [rel for rel in STATE_FILES if (state_root / rel).is_file()
             and (state_root / rel).stat().st_size <= MAX_FILE_BYTES]
    if not apply:
        return {"action": "save", "branch": branch, "dry_run": True, "files": files}
    tmp = Path(tempfile.mkdtemp(prefix="operator-state-"))
    wt = tmp / "wt"
    try:
        has = run(["git", "fetch", "--depth", "1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                  repo).returncode == 0
        if has:
            r = run(["git", "worktree", "add", "--detach", str(wt), f"origin/{branch}"], repo)
        else:
            r = run(["git", "worktree", "add", "--detach", str(wt)], repo)
        if r.returncode != 0:
            raise SystemExit(f"git worktree add failed: {r.stderr.strip()[:300]}")
        if not has:
            run(["git", "checkout", "--orphan", f"{branch}-init"], wt)
            run(["git", "rm", "-rf", "--quiet", "."], wt)
            (wt / "README.md").write_text(
                "Operator state for the ProSkills GitHub Actions (written by the workflows; never merged).\n",
                encoding="utf-8")
        for rel in files:
            dest = wt / "state" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(state_root / rel, dest)
        run(["git", "add", "-A", "."], wt)
        if run(["git", "diff", "--cached", "--quiet"], wt).returncode == 0:
            return {"action": "save", "branch": branch, "files": files, "committed": False}
        c = run(["git", "commit", "-q", "-m", message or "operator state"], wt)
        if c.returncode != 0:
            raise SystemExit(f"git commit failed: {c.stderr.strip()[:300]}")
        p = run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], wt)
        if p.returncode != 0:
            raise SystemExit(f"git push to {branch} failed: {p.stderr.strip()[:300]}")
        return {"action": "save", "branch": branch, "files": files, "committed": True}
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], repo)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Restore/save operator state on the operator-state branch")
    ap.add_argument("action", choices=("restore", "save"))
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--repo", type=Path, default=OPS)
    ap.add_argument("--message", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.action == "restore":
        out = restore(args.repo, args.branch)
    else:
        out = save(args.repo, args.branch, message=args.message, apply=not args.dry_run)
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
