"""Content-policy placeholders, scout_file open-issue cache, publish-time content-policy memory, topic sorts."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import content_policy as CP  # noqa: E402
import issue_flow as F  # noqa: E402
import scout_file as SF  # noqa: E402
import publish_candidates as PC  # noqa: E402
from sources import github_topics  # noqa: E402
from test_issue_flow import Base, src_rec  # noqa: E402


class TestContentPolicyPlaceholders(unittest.TestCase):
    def test_placeholders_and_technical_words_are_clean(self):
        for t in ("**CWE**: CWE-XXX — [Name]", "`Port xxx` + `Update rebuild plan: xxx`",
                  'platform_id="linkedin-xxx"', "No naked box, sphere or cylinder at hero scale.",
                  "【场景】xxx", "template text like [Company], TODO, XXX or lorem ipsum",
                  "[[cards/diesel-sfw-xxx-2010.md|SFW XXX]]", "use a naked domain", "visible to the naked eye"):
            self.assertEqual(CP.matches(t), [], t)

    def test_adult_uses_still_match(self):
        for t in ("download xxx videos", "naked women photos", "XXX movies", "adult xxx", "x-rated stories",
                  "nsfw", "porn site", "nude", "explicit sexual", "hentai", "onlyfans", "erotic roleplay"):
            self.assertTrue(CP.matches(t), t)

    def test_piracy_and_illegal_unchanged(self):
        self.assertTrue(CP.matches("warez keygen"))
        self.assertTrue(CP.matches("credit card fraud"))
        self.assertEqual(CP.matches("NSFW detection filter"), [])

    def test_policy_version_is_stable(self):
        self.assertEqual(CP.policy_version(), CP.policy_version())
        self.assertEqual(len(CP.policy_version()), 12)


class TestOpenIssueCache(Base):
    def setUp(self):
        super().setUp()
        self.gh.add_repo("Acme/Tools", {"skills/lint/SKILL.md": SKILL, "LICENSE": "MIT"}, stars=500)
        self.gh.add_repo("Solo/one", {"SKILL.md": SKILL}, stars=40)

    def test_full_then_delta(self):
        self.gh.add_issue(1, "old", "x")
        self.gh.add_issue(2, "old2", "y")
        cache = SF.OpenIssueCache(self.state / SF.OPEN_CACHE_NAME)
        issues, ok = cache.refresh(self.api)
        self.assertTrue(ok)
        self.assertEqual(cache.last_mode, "full")
        self.assertEqual(sorted(i["number"] for i in issues), [1, 2])
        # issue 2 closes, issue 3 opens, both after the sync
        later = "2999-01-01T00:00:00Z"
        self.gh.issues[2].update(state="closed", updated_at=later)
        self.gh.add_issue(3, "new", "z")["updated_at"] = later
        cache2 = SF.OpenIssueCache(self.state / SF.OPEN_CACHE_NAME)
        issues2, ok2 = cache2.refresh(self.api)
        self.assertTrue(ok2)
        self.assertEqual(cache2.last_mode, "delta")
        self.assertEqual(sorted(i["number"] for i in issues2), [1, 3])

    def test_filer_with_cache_files_and_is_idempotent(self):
        cache_path = self.state / SF.OPEN_CACHE_NAME

        def run():
            idx = SF.IssueIndex(self.state / SF.INDEX_NAME)
            f = SF.ScoutFiler(self.api, idx, apply=True, log=lambda m: None,
                              open_cache=SF.OpenIssueCache(cache_path))
            out = f.run([c for c in (F.normalize(r, "sources") for r in [src_rec("Acme", "Tools", "skills/lint")]) if c])
            idx.save()
            return out

        out = run()
        self.assertEqual(out["summary"]["counts"], {"create": 1})
        self.assertEqual(out["summary"]["open_listing"], "full")
        n = out["actions"][0]["issue"]
        self.gh.issues[n]["updated_at"] = "2999-01-01T00:00:00Z"  # GitHub bumps updated_at on create
        out2 = run()
        self.assertEqual(out2["summary"]["open_listing"], "delta")
        self.assertEqual(out2["summary"]["counts"], {})
        self.assertFalse(out2["summary"]["listing_incomplete"])


class TestPublishPolicyMemory(Base):
    def test_remembered_policy_skip_is_not_reselected_until_sha_or_policy_changes(self):
        self.gh.add_repo("A/one", {"s/SKILL.md": SKILL}, stars=300)
        self.gh.add_repo("B/two", {"s/SKILL.md": SKILL}, stars=100)
        out, _ = self.file([src_rec("A", "one", "s", stars=300), src_rec("B", "two", "s", stars=100)])
        a, b = (x["issue"] for x in out["actions"])
        self.reviewer().run(list(self.gh.issues.values()))
        sel = PC.select(self.api, self.state, catalog={"skills": []}, issues=list(self.gh.issues.values()))
        self.assertEqual([p["issue"] for p in sel["passed"]], [a, b])
        q = PC.queue_payload(sel)
        delta = {"skipped": [{"identity": "github:a/one::s", "reason": "content_policy", "hits": ["nsfw"]}]}
        self.assertEqual(PC.record_policy_skips(self.state, q, delta), ["github:a/one::s"])
        sel2 = PC.select(self.api, self.state, catalog={"skills": []}, issues=list(self.gh.issues.values()), limit=1)
        self.assertEqual([p["issue"] for p in sel2["passed"]], [b])
        reasons = {s["issue"]: s["reason"] for s in sel2["skipped"]}
        self.assertEqual(reasons[a], "content_policy_publish_hold")
        # a different policy version (matcher fixed) re-tries it
        p = self.state / PC.POLICY_SKIPS_NAME
        d = json.loads(p.read_text())
        d["policy_version"] = "old"
        p.write_text(json.dumps(d))
        sel3 = PC.select(self.api, self.state, catalog={"skills": []}, issues=list(self.gh.issues.values()))
        self.assertEqual([x["issue"] for x in sel3["passed"]], [a, b])

    def test_hold_label_excluded(self):
        i = {"number": 9001, "labels": [{"name": "candidate"}, {"name": "review:pass"}, {"name": "hold:content-policy"}]}
        self.assertEqual(PC.excluded(i), "held_label")


class FakeSearch:
    def __init__(self):
        self.calls = []

    def search(self, q, *, sort="stars", max_pages=3, per_page=100):
        self.calls.append((q, sort, max_pages))
        return [{"full_name": f"o/{sort}-{q[6:]}", "stars": 1}]


class TestTopicSorts(unittest.TestCase):
    def test_default_is_stars_only(self):
        s = FakeSearch()
        github_topics.collect(s, ["a"], 3)
        self.assertEqual(s.calls, [("topic:a", "stars", 3)])

    def test_updated_sort_added(self):
        s = FakeSearch()
        obs = github_topics.collect(s, ["a", "b"], 3, sorts=("stars", "updated"), updated_max_pages=1)
        self.assertEqual(s.calls, [("topic:a", "stars", 3), ("topic:a", "updated", 1),
                                   ("topic:b", "stars", 3), ("topic:b", "updated", 1)])
        self.assertEqual(len(obs), 4)


SKILL = __import__("fake_issues_github").SKILL

if __name__ == "__main__":
    unittest.main()
