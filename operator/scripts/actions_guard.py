#!/usr/bin/env python3
"""Gate for the ProSkills GitHub Actions workflows: decide whether this run may do anything.

A run proceeds only when ALL hold:
  * repository variable PROSKILLS_ACTIONS_ENABLED == 'true'  (env PROSKILLS_ACTIONS_ENABLED)
  * both GitHub App secrets are configured                    (env HAS_APP_ID / HAS_APP_KEY == 'true';
                                                                the workflow passes booleans, never values)
  * no OPEN issue in the repository carries the label `operator-lock` (manual pause / box-routine guard)

Otherwise it prints a clear reason and writes run=false to $GITHUB_OUTPUT; the job still succeeds.
The label check uses the workflow's own GITHUB_TOKEN (issues: read) through `gh`; if that check itself
fails the run is skipped (fail closed). No AI usage; never prints secret values.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Mapping

LOCK_LABEL = "operator-lock"


def _open_lock_issues(repo: str) -> list[int] | None:
    try:
        p = subprocess.run(["gh", "api", f"repos/{repo}/issues?state=open&labels={LOCK_LABEL}&per_page=5",
                            "--jq", "[.[].number]"], capture_output=True, text=True, timeout=60, check=False)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    try:
        return [int(x) for x in json.loads(p.stdout or "[]")]
    except ValueError:
        return None


def decide(env: Mapping[str, str], lock_check: Callable[[str], list[int] | None]) -> tuple[bool, str]:
    if env.get("PROSKILLS_ACTIONS_ENABLED", "").strip().lower() != "true":
        return False, "repository variable PROSKILLS_ACTIONS_ENABLED is not 'true' (actions disabled; box routines own the pipeline)"
    missing = [n for n, k in (("PROSKILLS_APP_ID", "HAS_APP_ID"), ("PROSKILLS_APP_PRIVATE_KEY", "HAS_APP_KEY"))
               if env.get(k, "").strip().lower() != "true"]
    if missing:
        return False, "missing secrets: " + ", ".join(missing)
    repo = env.get("GITHUB_REPOSITORY", "ProSkillsMD/proskills")
    locks = lock_check(repo)
    if locks is None:
        return False, f"could not check for open `{LOCK_LABEL}` issues (fail closed)"
    if locks:
        return False, f"open `{LOCK_LABEL}` issue(s) present: " + ", ".join(f"#{n}" for n in locks)
    return True, "enabled: variable set, secrets present, no operator-lock"


def main() -> int:
    ok, reason = decide(os.environ, _open_lock_issues)
    print(("RUN: " if ok else "SKIP: ") + reason, flush=True)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if ok else 'false'}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write(f"**Guard:** {'RUN' if ok else 'SKIP'} - {reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
