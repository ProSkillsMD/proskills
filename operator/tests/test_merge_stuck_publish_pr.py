"""merge_stuck_publish_pr.py with a fake `gh` runner. No network, no real gh calls."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import merge_stuck_publish_pr as m  # noqa: E402

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def pr(n, *, age_min=120, head=None, state="CLEAN", mergeable="MERGEABLE", draft=False, author="Asif2BD", base="main"):
    return {"number": n, "title": "operator: catalog publish", "headRefName": head or f"operator/catalog-publish-{n}",
            "baseRefName": base, "headRefOid": f"sha{n}", "isDraft": draft, "mergeable": mergeable,
            "mergeStateStatus": state, "author": {"login": author}, "url": f"https://x/pull/{n}",
            "createdAt": (NOW - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")}


class FakeGh:
    def __init__(self, prs, files=None, checks=None, merge_rc=0, list_rc=0):
        self.prs, self.files, self.checks = prs, files or {}, checks or {}
        self.merge_rc, self.list_rc = merge_rc, list_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        assert cmd[0] == "gh"
        if cmd[1:3] == ["pr", "list"]:
            return cp(self.list_rc, json.dumps(self.prs), "boom" if self.list_rc else "")
        n = int(cmd[3])
        if cmd[1:3] == ["pr", "view"] and "files" in cmd:
            return cp(0, json.dumps({"files": [{"path": p} for p in self.files.get(n, ["public/skills-catalog.json"])]}))
        if cmd[1:3] == ["pr", "checks"]:
            return self.checks.get(n, cp(1, "", "GraphQL: Resource not accessible by personal access token"))
        if cmd[1:3] == ["pr", "merge"]:
            return cp(self.merge_rc, "", "" if self.merge_rc == 0 else "merge blocked")
        if cmd[1:3] == ["pr", "view"]:
            return cp(0, json.dumps({"state": "MERGED", "mergedAt": "2026-09-26T06:00:05Z", "mergeCommit": {"oid": "abc"}}))
        raise AssertionError(cmd)

    def merges(self):
        return [c for c in self.calls if c[1:3] == ["pr", "merge"]]


class TestStuckMerge(unittest.TestCase):
    def test_dry_run_default_never_merges(self):
        g = FakeGh([pr(50)])
        out = m.run_check(apply=False, run=g, now=NOW)
        self.assertEqual(out["would_merge"], [50])
        self.assertEqual(g.merges(), [])
        self.assertTrue(out["dry_run"])

    def test_apply_squash_with_head_sha_guard(self):
        g = FakeGh([pr(50)])
        out = m.run_check(apply=True, run=g, now=NOW)
        self.assertEqual(out["merged"], [{"number": 50, "merge_sha": "abc", "merged_at": "2026-09-26T06:00:05Z"}])
        cmd = g.merges()[0]
        self.assertIn("--squash", cmd)
        self.assertEqual(cmd[cmd.index("--match-head-commit") + 1], "sha50")
        self.assertEqual(out["prs"][0]["checks_source"], "merge_state_clean")

    def test_skip_rules(self):
        prs = [pr(29, head="operator/catalog-publish-x"), pr(51, age_min=60), pr(52, state="UNSTABLE"),
               pr(53, mergeable="CONFLICTING"), pr(54, draft=True), pr(55, author="someone"),
               pr(56, base="dev"), pr(57), pr(58, head="operator/related-priority-token-optimizer"), pr(59), pr(60)]
        files = {57: ["public/skills-catalog.json", "app/page.tsx"]}
        checks = {59: cp(1, "build\tfail\t1m\thttps://x\n"), 60: cp(8, "build\tpending\t0\thttps://x\n")}
        g = FakeGh(prs, files=files, checks=checks)
        out = m.run_check(apply=True, run=g, now=NOW)
        reasons = {p["number"]: p.get("skip_reason") for p in out["prs"]}
        self.assertEqual(reasons, {51: "too_young", 52: "not_mergeable_clean", 53: "not_mergeable_clean",
                                   54: "draft", 55: "not_operator_author", 56: "base_not_main",
                                   57: "not_catalog_only", 59: "checks_failing", 60: "checks_pending"})
        self.assertEqual(g.merges(), [])
        self.assertNotIn(29, reasons)

    def test_passing_checks_recorded(self):
        g = FakeGh([pr(61)], checks={61: cp(0, "build\tpass\t1m\thttps://x\n")})
        out = m.run_check(apply=False, run=g, now=NOW)
        self.assertEqual(out["prs"][0]["checks_source"], "gh_pr_checks")

    def test_merge_failure_reported(self):
        g = FakeGh([pr(62)], merge_rc=1)
        out = m.run_check(apply=True, run=g, now=NOW)
        self.assertEqual(out["status"], "error")
        self.assertTrue(out["errors"][0].startswith("merge_failed #62"))

    def test_list_failure_never_acts(self):
        g = FakeGh([pr(63)], list_rc=1)
        out = m.run_check(apply=True, run=g, now=NOW)
        self.assertEqual(out["status"], "error")
        self.assertEqual(g.merges(), [])


if __name__ == "__main__":
    unittest.main()
