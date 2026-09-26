"""GitHub Actions layer: guard, operator-state branch, publish_run orchestrator, health summaries, workflows."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402
import actions_guard as AG  # noqa: E402
import actions_state as AS  # noqa: E402
import actions_health as AH  # noqa: E402
import publish_run as PR  # noqa: E402
import review as RV  # noqa: E402
from fake_issues_github import SKILL, FakeGH, make_api  # noqa: E402
from test_issue_flow import Base, src_rec  # noqa: E402

DHAKA = timezone(timedelta(hours=6))
WF = REPO_ROOT / ".github" / "workflows"


class TestGuard(unittest.TestCase):
    ENV = {"PROSKILLS_ACTIONS_ENABLED": "true", "HAS_APP_ID": "true", "HAS_APP_KEY": "true",
           "GITHUB_REPOSITORY": "ProSkillsMD/proskills"}

    def test_enabled(self):
        ok, why = AG.decide(self.ENV, lambda r: [])
        self.assertTrue(ok, why)

    def test_variable_not_true_skips(self):
        for v in ("", "false", "TRUE-ish", "1"):
            ok, why = AG.decide({**self.ENV, "PROSKILLS_ACTIONS_ENABLED": v}, lambda r: [])
            self.assertFalse(ok)
            self.assertIn("PROSKILLS_ACTIONS_ENABLED", why)

    def test_missing_secrets_skip_and_name_only(self):
        ok, why = AG.decide({**self.ENV, "HAS_APP_KEY": "false"}, lambda r: [])
        self.assertFalse(ok)
        self.assertIn("PROSKILLS_APP_PRIVATE_KEY", why)
        self.assertNotIn("PROSKILLS_APP_ID", why)

    def test_operator_lock_and_fail_closed(self):
        ok, why = AG.decide(self.ENV, lambda r: [12])
        self.assertFalse(ok)
        self.assertIn("operator-lock", why)
        ok, _ = AG.decide(self.ENV, lambda r: None)
        self.assertFalse(ok)

    def test_main_writes_output_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            with mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(out), "PROSKILLS_ACTIONS_ENABLED": ""}, clear=True):
                self.assertEqual(AG.main(), 0)
            self.assertEqual(out.read_text(), "run=false\n")


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, check=True)


class TestStateBranch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.remote = t / "remote.git"
        _git("init", "-q", "--bare", str(self.remote), cwd=t)
        self.repo = t / "repo"
        _git("init", "-q", "-b", "main", str(self.repo), cwd=t)
        for k, v in (("user.name", "t"), ("user.email", "t@example.invalid")):
            _git("config", k, v, cwd=self.repo)
        (self.repo / "f.txt").write_text("x")
        _git("add", ".", cwd=self.repo)
        _git("commit", "-q", "-m", "init", cwd=self.repo)
        _git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        _git("push", "-q", "origin", "main", cwd=self.repo)
        self.state = t / "state"
        (self.state / "scout").mkdir(parents=True)

    def test_first_save_creates_orphan_branch_then_restore(self):
        self.assertEqual(AS.restore(self.repo, state_root=self.state)["restored"], [])
        (self.state / "scout" / "issue-index.json").write_text('{"identities": {"a": 1}}')
        (self.state / "scout" / "repo-cache.json").write_text('{"big": "cache"}')  # not whitelisted
        r = AS.save(self.repo, state_root=self.state, message="t1")
        self.assertTrue(r["committed"])
        files = _git("ls-tree", "-r", "--name-only", "operator-state", cwd=self.remote).stdout.split()
        self.assertIn("state/scout/issue-index.json", files)
        self.assertNotIn("state/scout/repo-cache.json", files)
        self.assertEqual(_git("rev-list", "--count", "operator-state", cwd=self.remote).stdout.strip(), "1")
        # main is untouched
        self.assertEqual(_git("ls-tree", "-r", "--name-only", "main", cwd=self.remote).stdout.split(), ["f.txt"])
        # unchanged -> no commit; changed -> second commit on the same branch
        self.assertFalse(AS.save(self.repo, state_root=self.state)["committed"])
        (self.state / "scout" / "issue-index.json").write_text('{"identities": {"a": 2}}')
        self.assertTrue(AS.save(self.repo, state_root=self.state)["committed"])
        self.assertEqual(_git("rev-list", "--count", "operator-state", cwd=self.remote).stdout.strip(), "2")
        (self.state / "scout" / "issue-index.json").unlink()
        r = AS.restore(self.repo, state_root=self.state)
        self.assertEqual(r["restored"], ["scout/issue-index.json"])
        self.assertEqual(json.loads((self.state / "scout" / "issue-index.json").read_text())["identities"]["a"], 2)

    def test_refuses_main(self):
        for b in ("main", "master", "refs/heads/x", ""):
            with self.assertRaises(SystemExit):
                AS.save(self.repo, b, state_root=self.state)


class TestCommentOwnership(Base):
    def test_review_comment_by_other_account_gets_one_fresh_comment_then_stable(self):
        self.gh.add_repo("Acme/Tools", {"skills/lint/SKILL.md": SKILL})
        out, _ = self.file([src_rec("Acme", "Tools", "skills/lint")])
        n = out["actions"][0]["issue"]
        self.reviewer().run(list(self.gh.issues.values()))
        old = self.gh.issue_comments(n)[0]["id"]
        self.gh.forbidden_comments.add(old)              # written by the box account before cutover
        (self.state / RV.STATE_NAME).unlink()             # fresh Actions state
        self.gh.repos["acme/tools"]["files"]["skills/lint/SKILL.md"] = SKILL + "\nchanged\n"
        self.reviewer().run(list(self.gh.issues.values()), only=[n])
        cms = self.gh.issue_comments(n)
        self.assertEqual(len(cms), 2)
        (self.state / RV.STATE_NAME).unlink()
        res = self.reviewer().run(list(self.gh.issues.values()), only=[n])
        self.assertEqual(res["results"][0]["action"], "unchanged")   # last marker wins; no third comment
        self.assertEqual(len(self.gh.issue_comments(n)), 2)

    def test_installation_token_env_enables_body_edits(self):
        gh = FakeGH(can_push=False)
        api, _ = make_api(gh)
        self.assertFalse(api.can_edit_bodies())
        with mock.patch.dict("os.environ", {"PROSKILLS_ISSUES_WRITE": "true"}):
            self.assertTrue(api.can_edit_bodies())


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeWorld:
    """gh / git / npm / catalog_update / merge_stuck stand-ins for publish_run."""

    def __init__(self, website: Path, prs=None, view=None):
        self.website = website
        self.prs = prs or []
        self.view = view or {}
        self.calls: list[list[str]] = []
        self.live_ids: set[str] = set()
        self.merged = False

    def run(self, cmd, cwd=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "list"]:
            st = cmd[cmd.index("--state") + 1]
            return Proc(0, json.dumps([p for p in self.prs if st in ("all", p["state"].lower())]))
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "view"]:
            return Proc(0, json.dumps(self.view))
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "create"]:
            return Proc(0, "https://github.com/ProSkillsMD/website/pull/77\n")
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "merge"]:
            self.merged = True
            return Proc(0, "")
        if cmd[0] == "git":
            return Proc(0, "")
        if cmd[0] == "npm":
            return Proc(0, "built")
        if cmd[1].endswith("merge_stuck_publish_pr.py"):
            return Proc(0, json.dumps({"merged": []}) + "\n")
        if cmd[1].endswith("catalog_update.py"):
            cands = json.loads(Path(cmd[cmd.index("--candidates") + 1]).read_text())
            passed = cands.get("passed") or []
            added = [{"identity": c["identity"], "issue": c["issue"], "id": f"id-{c['issue']}",
                      "slug": f"slug-{c['issue']}", "category": "other"} for c in passed]
            cat = json.loads(Path(cmd[cmd.index("--catalog") + 1]).read_text())
            cat["skills"] += [{"id": a["id"], "slug": a["slug"]} for a in added]
            Path(cmd[cmd.index("--out") + 1]).write_text(json.dumps(cat))
            Path(cmd[cmd.index("--delta-out") + 1]).write_text(json.dumps({"added_map": added}))
            return Proc(0, "{}")
        raise AssertionError(f"unexpected command {cmd}")

    def http_get(self, url):
        if url.startswith(PR.LIVE_BASE + "/skills-catalog.json"):
            return 200, json.dumps({"skills": [{"id": i} for i in self.live_ids]}).encode()
        return 200, b"<html>"


class TestPublishRun(Base):
    def setUp(self):
        super().setUp()
        self.website = Path(self.tmp.name) / "website"
        (self.website / "public").mkdir(parents=True)
        (self.website / "public" / "skills-catalog.json").write_text(json.dumps({"skills": [{"id": "old"}], "total": 1}))
        self.art = Path(self.tmp.name) / "art"
        self.now = datetime(2026, 9, 26, 14, 44, tzinfo=DHAKA)
        self.gh.labels |= {"status:listed", "groot:published"}

    def passed(self, key, stars=10):
        self.gh.add_repo(key, {"s/SKILL.md": SKILL}, stars=stars)
        o, r = key.split("/")
        out, _ = self.file([src_rec(o, r, "s", stars=stars)])
        return out["actions"][0]["issue"]

    def pub(self, world, apply=True, **kw):
        api, _ = make_api(self.gh, self.clock)
        return PR.Publisher(api=api, website=self.website, author="app/proskills", apply=apply, run=world.run,
                            http_get=world.http_get, sleep=lambda s: None, now=lambda: self.now,
                            state_dir=self.state, art=self.art, poll_s=0, checks_timeout_s=0, verify_timeout_s=0,
                            log=lambda m: None, **kw)

    def merged_pr(self, n, added, when="2026-09-26T05:00:00Z", body=None):
        return {"number": n, "title": f"operator: catalog publish (+{added} skills → 9)", "state": "MERGED",
                "mergedAt": when, "headRefName": f"operator/catalog-publish-{n}",
                "body": body if body is not None else f'Diff stats: {{"added_count": {added}}}'}

    def green_view(self):
        return {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "abc",
                "headRefName": "operator/catalog-publish-x", "files": [{"path": "public/skills-catalog.json"}],
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}

    def test_marker_roundtrip_and_counts(self):
        m = PR.publish_marker([{"issue": 5, "id": "a@b", "slug": "s", "category": "c"}])
        self.assertNotIn("@", m)
        self.assertEqual(PR.parse_publish_marker("x " + m)[0]["id"], "a@b")
        self.assertEqual(PR.added_count({"body": 'x "added_count": 7 y'}), 7)
        self.assertEqual(PR.added_count({"body": "", "title": "publish (+4 skills → 742)"}), 4)
        self.assertEqual(PR.to_dhaka_date("2026-09-25T18:30:00Z"), "2026-09-26")

    def test_full_apply_flow_publishes_merges_verifies_and_closes(self):
        a = self.passed("A/one", 300)
        b = self.passed("B/two", 200)
        self.reviewer().run(list(self.gh.issues.values()))
        w = FakeWorld(self.website, prs=[self.merged_pr(70, 30)], view=self.green_view())
        w.live_ids = {"old", f"id-{a}", f"id-{b}"}
        res = self.pub(w).publish()
        self.assertEqual(res["steps"]["published_today"], 30)
        self.assertEqual(res["steps"]["pr"]["number"], 77)
        self.assertTrue(w.merged)
        self.assertEqual(sorted(res["steps"]["verify"]["listed"]), sorted([a, b]))
        for n in (a, b):
            labs = self.gh.labels_of(n)
            self.assertIn("status:listed", labs)
            self.assertIn("groot:published", labs)
            self.assertNotIn("publish:staged", labs)
            self.assertEqual(self.gh.issues[n]["state"], "closed")
            self.assertEqual(self.gh.issues[n]["state_reason"], "completed")
        create = next(c for c in w.calls if c[:3] == ["gh", "pr", "create"])
        body = create[create.index("--body") + 1]
        self.assertTrue(F.no_handles(body))
        self.assertEqual({i["issue"] for i in PR.parse_publish_marker(body)}, {a, b})
        self.assertIn('"added_count": 2', body)
        self.assertTrue(any(c[:3] == ["npm", "run", "build"] for c in w.calls))

    def test_dry_run_writes_nothing(self):
        self.passed("A/one")
        self.reviewer().run(list(self.gh.issues.values()))
        writes = len(self.gh.writes)
        w = FakeWorld(self.website, view=self.green_view())
        res = self.pub(w, apply=False).publish()
        self.assertIn("dry-run", res["stopped"])
        self.assertEqual(len(self.gh.writes), writes)
        self.assertFalse(any(c[0] == "git" or c[:3] == ["gh", "pr", "create"] for c in w.calls))

    def test_daily_cap_and_open_pr_stop(self):
        self.passed("A/one")
        self.reviewer().run(list(self.gh.issues.values()))
        w = FakeWorld(self.website, prs=[self.merged_pr(70, 60), self.merged_pr(71, 40),
                                         self.merged_pr(69, 50, when="2026-09-25T10:00:00Z")])
        res = self.pub(w).publish()
        self.assertIn("daily cap 100", res["stopped"])
        w2 = FakeWorld(self.website, prs=[{**self.merged_pr(72, 3), "state": "OPEN", "mergedAt": None}])
        self.assertIn("open publish PR", self.pub(w2).publish()["stopped"])

    def test_cap_limits_selection(self):
        for i, k in enumerate(("A/one", "B/two", "C/three")):
            self.passed(k, 100 - i)
        self.reviewer().run(list(self.gh.issues.values()))
        w = FakeWorld(self.website, prs=[self.merged_pr(70, 99)], view=self.green_view())
        res = self.pub(w, apply=False).publish()
        self.assertEqual(len(res["would_publish"]), 1)

    def test_not_merged_when_not_catalog_only_or_failing(self):
        a = self.passed("A/one")
        self.reviewer().run(list(self.gh.issues.values()))
        v = self.green_view()
        v["files"].append({"path": "app/page.tsx"})
        w = FakeWorld(self.website, view=v)
        res = self.pub(w).publish()
        self.assertFalse(w.merged)
        self.assertEqual(res["steps"]["merge"]["reason"], "not_mergeable_by_rule")
        self.assertIn("publish:staged", self.gh.labels_of(a))
        self.assertEqual(self.gh.issues[a]["state"], "open")
        v2 = self.green_view()
        v2["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        pub = self.pub(FakeWorld(self.website, view=v2))
        self.assertFalse(pub.wait_and_merge(5)["merged"])
        v3 = self.green_view()
        v3["statusCheckRollup"] = [{"status": "IN_PROGRESS", "conclusion": None}]
        self.assertIn("checks_timeout", self.pub(FakeWorld(self.website, view=v3)).wait_and_merge(5)["reason"])

    def test_reconcile_closes_staged_items_once_live(self):
        a = self.passed("A/one")
        self.reviewer().run(list(self.gh.issues.values()))
        self.gh.issues[a]["labels"].append({"name": "publish:staged"})
        body = PR.publish_marker([{"issue": a, "id": "id-a", "slug": "s", "category": "other"}])
        w = FakeWorld(self.website, prs=[self.merged_pr(70, 1, body=body)])
        pub = self.pub(w)
        self.assertEqual(pub.reconcile()["not_live_yet"], [a])
        self.assertEqual(self.gh.issues[a]["state"], "open")
        w.live_ids = {"id-a"}
        self.assertEqual(pub.reconcile()["listed"], [a])
        self.assertEqual(self.gh.issues[a]["state"], "closed")

    def test_protected_never_labelled(self):
        self.gh.add_issue(2396, "x", "y", labels=["candidate", "publish:staged"])
        w = FakeWorld(self.website)
        w.live_ids = {"i"}
        res = self.pub(w).finish_items([{"issue": 2396, "id": "i", "slug": "s", "category": "c"}],
                                      {"skills": [{"id": "i"}]})
        self.assertEqual(res["skipped"], [2396])
        self.assertNotIn("status:listed", self.gh.labels_of(2396))

    def test_cap_argument_cannot_exceed_100(self):
        with self.assertRaises(SystemExit):
            PR.main(["--website", "/tmp", "--author", "x", "--cap", "150"])


class TestHealth(unittest.TestCase):
    def test_summarize_tolerates_missing_and_has_no_handles(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"results": [{"action": "comment", "verdict": "review:pass"}]}))
            s = AH.summarize(str(Path(d) / "missing.json"), str(p), None)
        self.assertIn("scout_file: no output", s)
        self.assertIn("review:pass", s)
        self.assertTrue(F.no_handles(s))

    def test_report_with_fake_gh(self):
        def run(cmd, env=None):
            if cmd[1] == "api" and "search/issues" in cmd:
                return Proc(0, "3")
            if cmd[1:3] == ["pr", "list"]:
                return Proc(0, json.dumps([{"number": 5, "title": "+2 skills", "body": '"added_count": 2', "state": "MERGED",
                                            "mergedAt": datetime.now(timezone.utc).isoformat(),
                                            "headRefName": "operator/catalog-publish-1"}]))
            return Proc(0, "[]")
        s = AH.report(run=run, http_get=lambda u: (200, b'{"skills": [1, 2]}'))
        self.assertIn("| skills published today (Asia/Dhaka, cap 100) | 2 |", s)
        self.assertIn("| live catalog skills | 2 |", s)


class TestWorkflows(unittest.TestCase):
    NAMES = ("proskills-intake.yml", "proskills-publish.yml", "proskills-health.yml")

    def text(self, name):
        return (WF / name).read_text(encoding="utf-8")

    def test_crons(self):
        self.assertIn('cron: "14 * * * *"', self.text("proskills-intake.yml"))
        self.assertIn('cron: "44 * * * *"', self.text("proskills-publish.yml"))
        self.assertIn('cron: "1 3 * * *"', self.text("proskills-health.yml"))

    def test_guarded_pinned_minimal(self):
        for name in self.NAMES:
            t = self.text(name)
            self.assertIn("workflow_dispatch:", t)
            self.assertIn("\npermissions:\n  contents: read\n", t)
            self.assertIn("concurrency:", t)
            self.assertIn("vars.PROSKILLS_ACTIONS_ENABLED", t)
            self.assertIn("if: needs.guard.outputs.run == 'true'", t)
            for uses in re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*(\S+)", t):
                self.assertRegex(uses, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", f"{name}: unpinned {uses}")
            self.assertIn("owner: ProSkillsMD", t)
            self.assertIn("repositories: proskills,website", t)
            # secrets only reach the token action / guard booleans, never a run: script
            for line in t.splitlines():
                if "secrets." in line:
                    self.assertTrue(re.search(r"(app-id|private-key|HAS_APP_ID|HAS_APP_KEY):", line), line)
            runs = "\n".join(l for l in t.splitlines() if "run:" in l or "python3" in l)
            self.assertNotRegex(runs, r"(?i)openai|anthropic|ai_review")
        self.assertIn("group: proskills-operator-pipeline", self.text("proskills-intake.yml"))
        self.assertIn("group: proskills-operator-pipeline", self.text("proskills-publish.yml"))

    def test_intake_and_publish_steps(self):
        t = self.text("proskills-intake.yml")
        for s in ("scout.py --write", "scout_file.py --apply", "review.py --apply", "actions_state.py save"):
            self.assertIn(s, t)
        p = self.text("proskills-publish.yml")
        self.assertIn("publish_run.py --apply", p)
        self.assertIn("--cap 100", p)
        self.assertIn("repository: ProSkillsMD/website", p)


if __name__ == "__main__":
    unittest.main()
