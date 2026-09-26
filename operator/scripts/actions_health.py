#!/usr/bin/env python3
"""Markdown job summaries for the ProSkills GitHub Actions workflows (read-only; no AI).

  summarize  render the JSON outputs of scout_file / review / publish_run (missing files are tolerated)
  report     daily health: candidate issues by review label, open publish PRs, skills published today
             (Asia/Dhaka), live catalog total, last intake/publish workflow runs

Output is Markdown on stdout (the workflow appends it to $GITHUB_STEP_SUMMARY). Issue numbers are written
without a leading '#' and no @-handles are ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

DHAKA = timezone(timedelta(hours=6))
REPO = "ProSkillsMD/proskills"
WEBSITE = "ProSkillsMD/website"
LABELS = ("review:pass", "review:license-review", "review:hold-critical", "review:needs-ai", "review:reject",
          "review:large-collection", "review:hold-ai", "publish:staged")


def _load(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clean(v: Any) -> str:
    return str(v).replace("@", "\uff20").replace("#", "\uff03").replace("|", "/")


def summarize(scout_file: str | None, review: str | None, publish: str | None) -> str:
    out = [f"## ProSkills run summary ({datetime.now(DHAKA):%Y-%m-%d %H:%M} Asia/Dhaka)", ""]
    sf = _load(scout_file)
    if scout_file:
        if sf is None:
            out.append("- scout_file: no output (step skipped or failed)")
        else:
            s = sf.get("summary") or {}
            acts = Counter(a.get("action") for a in sf.get("actions") or [])
            out.append(f"- scout_file: actions {dict(acts)}; filed today {s.get('filed_today')}; "
                       f"unreviewed {s.get('unreviewed_candidates')}; stopped: {_clean(s.get('stopped'))}")
    rv = _load(review)
    if review:
        if rv is None:
            out.append("- review: no output (step skipped or failed)")
        else:
            acts = Counter(r.get("action") for r in rv.get("results") or [])
            verdicts = Counter(r.get("verdict") for r in rv.get("results") or [] if r.get("action") != "unchanged")
            out.append(f"- review: actions {dict(acts)}; changed verdicts {dict(verdicts)}")
    pb = _load(publish)
    if publish:
        if pb is None:
            out.append("- publish: no output (step skipped or failed)")
        else:
            steps = pb.get("steps") or {}
            pr = (steps.get("pr") or {}).get("number")
            out.append(f"- publish: published today before run {steps.get('published_today')}; "
                       f"remaining {pb.get('remaining_today')}; PR {pr or '-'}; merge {_clean(steps.get('merge'))}; "
                       f"verify {_clean(steps.get('verify'))}; stopped: {_clean(pb.get('stopped'))}")
    return "\n".join(out) + "\n"


Runner = Callable[[Sequence[str], dict | None], "subprocess.CompletedProcess[str]"]


def _run(cmd: Sequence[str], env: dict | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False, timeout=300,
                          env={**os.environ, **(env or {})})


def _gh(run: Runner, args: list[str], env: dict | None = None) -> Any:
    p = run(["gh", *args], env)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout or "null")
    except ValueError:
        return None


def report(run: Runner = _run, http_get: Callable[[str], tuple[int, bytes]] | None = None) -> str:
    import publish_run as PR
    http_get = http_get or PR._http_get
    today = datetime.now(DHAKA).date().isoformat()
    out = [f"## ProSkills daily health ({datetime.now(DHAKA):%Y-%m-%d %H:%M} Asia/Dhaka)", "",
           "| Metric | Value |", "|---|---|"]
    for lab in ("candidate",) + LABELS:
        n = _gh(run, ["api", "-X", "GET", "search/issues", "-f", f"q=repo:{REPO} is:issue is:open label:\"{lab}\"",
                      "--jq", ".total_count"])
        out.append(f"| open `{lab}` | {n if n is not None else 'n/a'} |")
    unrev = _gh(run, ["api", "-X", "GET", "search/issues", "-f",
                      f"q=repo:{REPO} is:issue is:open label:candidate -label:review:pass -label:review:license-review "
                      "-label:review:hold-critical -label:review:needs-ai -label:review:reject "
                      "-label:review:large-collection -label:review:hold-ai", "--jq", ".total_count"])
    out.append(f"| open candidates without a review label | {unrev if unrev is not None else 'n/a'} |")
    prs = _gh(run, ["pr", "list", "-R", WEBSITE, "--state", "all", "--limit", "100", "--json",
                    "number,title,body,state,mergedAt,headRefName"]) or []
    prs = [p for p in prs if str(p.get("headRefName") or "").startswith(PR.HEAD_PREFIX)]
    merged_today = [p for p in prs if PR.to_dhaka_date(p.get("mergedAt")) == today]
    out.append(f"| skills published today (Asia/Dhaka, cap 100) | {sum(PR.added_count(p) for p in merged_today)} |")
    out.append(f"| open publish PRs | {', '.join(str(p['number']) for p in prs if p.get('state') == 'OPEN') or 'none'} |")
    st, body = http_get(f"{PR.LIVE_BASE}/skills-catalog.json")
    total = None
    if st == 200:
        try:
            total = len(json.loads(body.decode('utf-8')).get("skills") or [])
        except ValueError:
            total = None
    out.append(f"| live catalog skills | {total if total is not None else 'n/a'} |")
    tok = os.environ.get("RUNS_TOKEN")
    for wf in ("proskills-intake.yml", "proskills-publish.yml"):
        runs = _gh(run, ["api", f"repos/{REPO}/actions/workflows/{wf}/runs?per_page=24", "--jq",
                         "[.workflow_runs[] | {c: .conclusion, t: .created_at}]"], {"GH_TOKEN": tok} if tok else None)
        if runs is None:
            out.append(f"| {wf} last 24 runs | n/a |")
        else:
            c = Counter(r.get("c") or "running" for r in runs)
            out.append(f"| {wf} last 24 runs | {dict(c)} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ProSkills Actions job summaries")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("--scout-file")
    s.add_argument("--review")
    s.add_argument("--publish")
    sub.add_parser("report")
    args = ap.parse_args(argv)
    if args.cmd == "summarize":
        sys.stdout.write(summarize(args.scout_file, args.review, args.publish))
    else:
        sys.stdout.write(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
