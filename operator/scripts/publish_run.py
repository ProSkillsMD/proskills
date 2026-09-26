#!/usr/bin/env python3
"""Deterministic issue-based publisher (GitHub Actions `proskills-publish.yml`; usable on the box too).

One run, in order (each step logs one line; the final JSON summary goes to stdout / --out):

 1. reconcile   merged `operator/catalog-publish-*` website PRs (last 3 days) carry a
                `<!-- proskills:publish v1 items=[...] -->` marker (issue -> catalog id/slug/category).
                Items whose issue is still open and whose id is in the LIVE catalog (and page HTTP 200) get
                `status:listed` + `groot:published`, lose `publish:staged`, and are closed as completed.
 2. stuck PRs   merge_stuck_publish_pr.py --apply (unchanged rules: catalog-only, MERGEABLE/CLEAN, green,
                > 90 min old, operator head branch, given author).
 3. gates       stop if any operator publish PR is still open; daily cap: 100 per Asia/Dhaka day counted
                from merged operator publish PRs (added_count in the PR body / "+N skills" in the title).
 4. select      publish_candidates.select(): open review:pass issues whose live sha == reviewed sha.
 5. stage       catalog_update (append-only) against the website checkout's public/skills-catalog.json.
 6. build       copy staged catalog into the website checkout, `npm run build` (production build).
 7. PR          branch operator/catalog-publish-<Dhaka ts>, commit, push, open PR (body: diff stats + marker);
                label the issues publish:staged.
 8. merge       poll until catalog-only + MERGEABLE + CLEAN + all checks green, then
                `gh pr merge --squash --match-head-commit <sha>`; timeout -> left for step 2 next hour.
 9. verify      poll the live catalog for the new ids, check pages, then label/close as in step 1.

Dry-run by default (steps 1-5 read-only, prints the plan). --apply performs writes. No AI usage.
Never touches website PR #29 or protected/critical issues; never @-mentions anyone.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402
import publish_candidates as PC  # noqa: E402

WEBSITE_REPO = "ProSkillsMD/website"
HEAD_PREFIX = "operator/catalog-publish-"
CATALOG_REL = "public/skills-catalog.json"
LIVE_BASE = "https://proskills.md"
DHAKA = timezone(timedelta(hours=6))
DAILY_CAP = 100
PER_RUN = 40
PROTECTED_WEBSITE_PRS = frozenset({29, *range(1, 16)})
MARK_RE = re.compile(r"<!--\s*proskills:publish v1 items=(\[.*?\])\s*-->", re.S)
ADDED_RE = re.compile(r'"added_count"\s*:\s*(\d+)')
TITLE_RE = re.compile(r"\+(\d+) skills?")
OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

Runner = Callable[[Sequence[str], Path | None], "subprocess.CompletedProcess[str]"]
HttpGet = Callable[[str], tuple[int, bytes]]


def _run(cmd: Sequence[str], cwd: Path | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          check=False, timeout=1800)


def _http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "proskills-operator/1.0", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def now_dhaka() -> datetime:
    return datetime.now(DHAKA)


def to_dhaka_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(DHAKA).date().isoformat()
    except ValueError:
        return None


def added_count(pr: dict[str, Any]) -> int:
    m = ADDED_RE.search(pr.get("body") or "")
    if m:
        return int(m.group(1))
    m = TITLE_RE.search(pr.get("title") or "")
    return int(m.group(1)) if m else 0


def publish_marker(items: list[dict[str, Any]]) -> str:
    slim = [{"issue": i.get("issue"), "id": i.get("id"), "slug": i.get("slug"), "category": i.get("category")}
            for i in items if i.get("issue")]
    return "<!-- proskills:publish v1 items=" + json.dumps(slim, separators=(",", ":")).replace("@", "\\u0040") + " -->"


def parse_publish_marker(body: str | None) -> list[dict[str, Any]]:
    m = MARK_RE.search(body or "")
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("issue") and i.get("id")]


class Publisher:
    def __init__(self, *, api: F.IssueRepo | None, website: Path, author: str, apply: bool,
                 run: Runner = _run, http_get: HttpGet = _http_get, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = now_dhaka, cap: int = DAILY_CAP, per_run: int = PER_RUN,
                 build: bool = True, checks_timeout_s: int = 1500, verify_timeout_s: int = 1200,
                 poll_s: int = 30, state_dir: Path = F.STATE_DIR, website_repo: str = WEBSITE_REPO,
                 scripts: Path = Path(__file__).resolve().parent, art: Path = scout.ART,
                 log: Callable[[str], None] | None = None):
        self.api, self.website, self.author, self.apply = api, website, author, apply
        self.run, self.http_get, self.sleep, self.now = run, http_get, sleep, now
        self.cap, self.per_run, self.build = cap, per_run, build
        self.checks_timeout_s, self.verify_timeout_s, self.poll_s = checks_timeout_s, verify_timeout_s, poll_s
        self.state_dir, self.website_repo, self.scripts, self.art = state_dir, website_repo, scripts, art
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.summary: dict[str, Any] = {"apply": apply, "steps": {}}

    # ------------------------------------------------------------------ helpers
    def gh_json(self, args: list[str]) -> Any:
        p = self.run(["gh", *args], None)
        if p.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(p.stderr or '').strip()[:300]}")
        return json.loads(p.stdout or "null")

    def list_publish_prs(self, state: str) -> list[dict[str, Any]]:
        prs = self.gh_json(["pr", "list", "-R", self.website_repo, "--state", state, "--limit", "100",
                            "--json", "number,title,body,state,mergedAt,createdAt,headRefName,author"]) or []
        return [p for p in prs if str(p.get("headRefName") or "").startswith(HEAD_PREFIX)
                and int(p.get("number") or 0) not in PROTECTED_WEBSITE_PRS]

    def live_catalog(self) -> dict[str, Any] | None:
        st, body = self.http_get(f"{LIVE_BASE}/skills-catalog.json?ts={int(time.time())}")
        if st != 200:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            return None

    def page_ok(self, item: dict[str, Any]) -> bool:
        st, _ = self.http_get(f"{LIVE_BASE}/skills/{item.get('category') or 'other'}/{item.get('slug')}")
        return st == 200

    # ------------------------------------------------------------------ 1/9 label + close
    def finish_items(self, items: list[dict[str, Any]], catalog: dict[str, Any] | None) -> dict[str, Any]:
        res = {"listed": [], "not_live_yet": [], "skipped": []}
        if not items or catalog is None or self.api is None:
            res["not_live_yet"] = [i["issue"] for i in items]
            return res
        live_ids = {s.get("id") for s in catalog.get("skills") or []}
        for it in items:
            n = int(it["issue"])
            if it.get("id") not in live_ids or not self.page_ok(it):
                res["not_live_yet"].append(n)
                continue
            issue = self.api.get_issue(n)
            labs = F.issue_labels(issue)
            if issue.get("state") != "open" or F.LABEL_CANDIDATE not in labs:
                res["skipped"].append(n)
                continue
            if n in F.NEVER_TOUCH:
                res["skipped"].append(n)
                continue
            if self.apply:
                self.api.add_labels(n, ["status:listed", "groot:published"])
                if F.LABEL_STAGED in labs:
                    self.api.remove_label(n, F.LABEL_STAGED)
                self.api.edit_issue(n, state="closed", state_reason="completed")
            res["listed"].append(n)
        return res

    def reconcile(self) -> dict[str, Any]:
        cutoff = (self.now() - timedelta(days=3)).date().isoformat()
        items: list[dict[str, Any]] = []
        for pr in self.list_publish_prs("merged"):
            d = to_dhaka_date(pr.get("mergedAt"))
            if d and d >= cutoff:
                items += parse_publish_marker(pr.get("body"))
        if not items:
            return {"items": 0}
        open_staged = {int(i["number"]) for i in self.api.list_issues(state="open", labels=F.LABEL_STAGED)} \
            if self.api else set()
        pending = [i for i in items if int(i["issue"]) in open_staged]
        out = self.finish_items(pending, self.live_catalog() if pending else None)
        out["items"] = len(items)
        return out

    # ------------------------------------------------------------------ 2 stuck PRs
    def merge_stuck(self) -> dict[str, Any]:
        cmd = [sys.executable, str(self.scripts / "merge_stuck_publish_pr.py"), "--author", self.author,
               "--repo", self.website_repo] + (["--apply"] if self.apply else [])
        p = self.run(cmd, None)
        line = (p.stdout or "").strip().splitlines()[-1:] or ["{}"]
        try:
            return json.loads(line[0])
        except ValueError:
            return {"rc": p.returncode, "raw": (p.stdout or "")[-300:]}

    # ------------------------------------------------------------------ 3 gates
    def published_today(self) -> int:
        today = self.now().date().isoformat()
        return sum(added_count(p) for p in self.list_publish_prs("merged") if to_dhaka_date(p.get("mergedAt")) == today)

    # ------------------------------------------------------------------ 8 merge
    def wait_and_merge(self, number: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.checks_timeout_s
        last: dict[str, Any] = {}
        while True:
            v = self.gh_json(["pr", "view", str(number), "-R", self.website_repo, "--json",
                              "state,mergeable,mergeStateStatus,headRefOid,files,statusCheckRollup,headRefName"])
            files = sorted(f.get("path") for f in v.get("files") or [])
            checks = v.get("statusCheckRollup") or []
            bad = [c for c in checks if (c.get("conclusion") or c.get("state") or "").upper() in
                   {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}]
            pending = [c for c in checks if (c.get("status") or "COMPLETED").upper() != "COMPLETED"
                       or (c.get("state") or "").upper() in {"PENDING", "EXPECTED"}]
            last = {"state": v.get("state"), "mergeable": v.get("mergeable"), "merge_state": v.get("mergeStateStatus"),
                    "files": files, "failing": len(bad), "pending": len(pending)}
            if v.get("state") != "OPEN" or files != [CATALOG_REL] or bad \
                    or not str(v.get("headRefName") or "").startswith(HEAD_PREFIX):
                return {"merged": False, "reason": "not_mergeable_by_rule", **last}
            if v.get("mergeable") == "MERGEABLE" and v.get("mergeStateStatus") == "CLEAN" and not pending:
                p = self.run(["gh", "pr", "merge", str(number), "-R", self.website_repo, "--squash",
                              "--match-head-commit", str(v.get("headRefOid"))], None)
                if p.returncode != 0:
                    return {"merged": False, "reason": "merge_failed", "stderr": (p.stderr or "")[-300:], **last}
                return {"merged": True, **last}
            if time.monotonic() >= deadline:
                return {"merged": False, "reason": "checks_timeout (left for merge_stuck_publish_pr next run)", **last}
            self.sleep(self.poll_s)

    def wait_live(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.verify_timeout_s
        want = {i["id"] for i in items}
        while True:
            cat = self.live_catalog()
            if cat is not None and want <= {s.get("id") for s in cat.get("skills") or []}:
                return cat
            if time.monotonic() >= deadline:
                return cat
            self.sleep(max(self.poll_s, 60))

    # ------------------------------------------------------------------ main flow
    def step(self, name: str, value: Any) -> Any:
        self.summary["steps"][name] = value
        self.log(f"[publish] {name}: {json.dumps(value, default=str)[:400]}")
        return value

    def publish(self) -> dict[str, Any]:
        s = self.summary
        if self.api is not None:
            self.step("reconcile", self.reconcile())
        self.step("merge_stuck", self.merge_stuck())
        open_prs = self.list_publish_prs("open")
        if open_prs:
            s["stopped"] = f"open publish PR(s) pending: {[p['number'] for p in open_prs]}"
            return s
        done = self.step("published_today", self.published_today())
        remaining = max(0, self.cap - done)
        s["remaining_today"] = remaining
        if remaining <= 0:
            s["stopped"] = f"daily cap {self.cap} reached (Asia/Dhaka day)"
            return s
        catalog_path = self.website / CATALOG_REL
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        issues = self.api.list_issues(state="open", labels="review:pass")
        ch = cache = None
        if any((F.parse_block(i.get("body")) or {}).get("psk-id", "").startswith("clawhub:") for i in issues):
            from review import load_clawhub
            c, feed, cache = load_clawhub(self.state_dir)
            ch = (c, feed)
        sel = PC.select(self.api, self.state_dir, limit=min(self.per_run, remaining), catalog=catalog,
                        clawhub=ch, issues=issues)
        if cache is not None:
            cache.save()
        self.step("select", {"passed": [p["issue"] for p in sel["passed"]], "skipped": len(sel["skipped"])})
        if not sel["passed"]:
            s["stopped"] = "no review:pass issues ready"
            return s
        self.art.mkdir(parents=True, exist_ok=True)
        stamp = self.now().strftime("%Y%m%d-%H%M")
        queue = self.art / f"issue-queue-actions-{stamp}.json"
        F.save_json(queue, PC.queue_payload(sel))
        staged, delta = self.art / f"staged-{stamp}.json", self.art / f"delta-{stamp}.json"
        p = self.run([sys.executable, str(self.scripts / "catalog_update.py"), "--apply", "--candidates", str(queue),
                      "--catalog", str(catalog_path), "--out", str(staged), "--delta-out", str(delta)], None)
        if p.returncode != 0 or not delta.is_file():
            s["stopped"] = f"catalog_update failed rc={p.returncode}: {(p.stderr or p.stdout or '')[-300:]}"
            s["error"] = True
            return s
        d = json.loads(delta.read_text(encoding="utf-8"))
        items = [i for i in d.get("added_map") or [] if i.get("issue")][:remaining]
        self.step("stage", {"added": len(items), "issues": [i["issue"] for i in items]})
        if not items:
            s["stopped"] = "nothing new to add after catalog_update"
            return s
        if not self.apply:
            s["stopped"] = "dry-run: would build, open a publish PR, merge when green, verify and close"
            s["would_publish"] = [i["issue"] for i in items]
            return s
        old_total = len(catalog.get("skills") or [])
        new_cat = json.loads(staged.read_text(encoding="utf-8"))
        keep = {i["id"] for i in items}
        old_ids = {x.get("id") for x in catalog.get("skills") or []}
        new_cat["skills"] = [x for x in new_cat.get("skills") or [] if x.get("id") in old_ids or x.get("id") in keep]
        if "total" in new_cat:
            new_cat["total"] = len(new_cat["skills"])
        branch = f"{HEAD_PREFIX}{stamp}"
        g = lambda *a: self.run(["git", *a], self.website)  # noqa: E731
        if g("status", "--porcelain").stdout.strip():
            s["stopped"], s["error"] = "website checkout is dirty", True
            return s
        g("checkout", "-q", "-b", branch)
        catalog_path.write_text(json.dumps(new_cat, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if self.build:
            b = self.run(["npm", "run", "build"], self.website)
            self.step("build", {"rc": b.returncode})
            if b.returncode != 0:
                g("checkout", "-q", "--", CATALOG_REL)
                s["stopped"], s["error"] = f"production build failed: {(b.stderr or b.stdout or '')[-400:]}", True
                return s
        new_total = len(new_cat["skills"])
        stats = {"old_total": old_total, "new_total": new_total, "added_count": new_total - old_total, "removed_count": 0,
                 "added_ids_sample": [i["id"] for i in items][:20], "removed_ids_sample": []}
        title = f"operator: catalog publish actions-{self.now().strftime('%Y-%m-%d-%H%M')} (+{new_total - old_total} skills → {new_total})"
        body = ("## Summary\n- Operator staged catalog append from review:pass candidate issues "
                "(no existing skill id/slug/repo overwritten).\n"
                f"- Diff stats: {json.dumps(stats)}\n\n## Notes\n- Opened by publish_run.py (GitHub Actions); merged "
                "automatically only when catalog-only, MERGEABLE/CLEAN and all checks are green.\n\n"
                + publish_marker(items) + "\n")
        assert F.no_handles(body)
        g("add", CATALOG_REL)
        c = g("commit", "-q", "-m", title)
        push = g("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        if c.returncode != 0 or push.returncode != 0:
            s["stopped"], s["error"] = f"commit/push failed: {(c.stderr or push.stderr or '')[-300:]}", True
            return s
        pr = self.run(["gh", "pr", "create", "-R", self.website_repo, "--base", "main", "--head", branch,
                       "--title", title, "--body", body], self.website)
        if pr.returncode != 0:
            s["stopped"], s["error"] = f"gh pr create failed: {(pr.stderr or '')[-300:]}", True
            return s
        m = re.search(r"/pull/(\d+)", pr.stdout or "")
        number = int(m.group(1)) if m else 0
        self.step("pr", {"number": number, "branch": branch, "added": len(items)})
        if self.api is not None:
            self.step("mark_staged", PC.mark_staged(self.api, [int(i["issue"]) for i in items], apply=True))
        merged = self.step("merge", self.wait_and_merge(number) if number else {"merged": False, "reason": "no_pr_number"})
        if not merged.get("merged"):
            s["stopped"] = "publish PR not merged this run"
            return s
        cat = self.wait_live(items)
        self.step("verify", self.finish_items(items, cat))
        return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Issue-based publisher (review:pass issues -> website catalog PR)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--website", type=Path, required=True, help="website checkout (ProSkillsMD/website)")
    ap.add_argument("--author", required=True, help="login that opens publish PRs (for merge_stuck_publish_pr)")
    ap.add_argument("--cap", type=int, default=DAILY_CAP)
    ap.add_argument("--per-run", type=int, default=PER_RUN)
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--checks-timeout-min", type=float, default=25)
    ap.add_argument("--verify-timeout-min", type=float, default=20)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.cap > DAILY_CAP:
        ap.error(f"--cap may not exceed {DAILY_CAP}")
    client = scout.GitHubClient(scout.get_gh_token(), throttle=scout.AdaptiveThrottle(4))
    pub = Publisher(api=F.IssueRepo(client), website=args.website.resolve(), author=args.author, apply=args.apply,
                    cap=args.cap, per_run=args.per_run, build=not args.no_build,
                    checks_timeout_s=int(args.checks_timeout_min * 60), verify_timeout_s=int(args.verify_timeout_min * 60))
    try:
        res = pub.publish()
    except Exception as e:  # noqa: BLE001 - summarised, then non-zero exit
        res = {**pub.summary, "error": True, "stopped": f"{type(e).__name__}: {str(e)[:300]}"}
    text = json.dumps(res, indent=2, default=str)
    print(text, flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
