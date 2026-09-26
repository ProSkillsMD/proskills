"""Queue ordering / identity helpers of scout.py and the importlib loading style. No network."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scout  # noqa: E402


def cand(issue, owner, repo, sub=None, stars=0, **kw):
    c = {"issue": issue, "owner": owner, "repo": repo, "subpath": sub, "stars": stars,
         "identity": scout.identity_for(owner, repo, sub), "license_tier": "pass"}
    c.update(kw)
    return c


class TestOrdering(unittest.TestCase):
    def test_max_five_per_repo(self):
        passed = [cand(1, "big", "mono", f"skills/s{i}", stars=900) for i in range(12)]
        passed += [cand(2, "small", "one", None, stars=5)]
        out = scout.order_passed(passed, 40, 5)
        self.assertEqual(sum(1 for p in out if p["repo"] == "mono"), 5)
        self.assertIn("github:small/one", [p["identity"] for p in out])

    def test_round_robin_by_issue_then_stars(self):
        passed = [cand(10, "a", "mono", f"s{i}", stars=1000) for i in range(4)]
        passed += [cand(11, "b", "mono", f"s{i}", stars=500) for i in range(3)]
        passed += [cand(12, "c", "single", None, stars=10)]
        out = [p["identity"] for p in scout.order_passed(passed, 40, 5)]
        # first round: best skill of each issue, ordered by stars; then second round ...
        self.assertEqual(out[:3], ["github:a/mono::s0", "github:b/mono::s0", "github:c/single"])
        self.assertEqual(out[3:5], ["github:a/mono::s1", "github:b/mono::s1"])
        self.assertEqual(len(out), 8)

    def test_pure_star_sort_is_not_used(self):
        passed = [cand(1, "a", "r", f"s{i}", stars=1000) for i in range(5)] + [cand(2, "b", "r", None, stars=1)]
        out = scout.order_passed(passed, 2, 5)
        self.assertEqual([p["issue"] for p in out], [1, 2])

    def test_repo_in_catalog_goes_last_and_total_cap(self):
        passed = [cand(1, "old", "repo", "s1", stars=9999, repo_in_catalog=True),
                  cand(2, "new", "repo", None, stars=1)]
        out = scout.order_passed(passed, 40, 5)
        self.assertEqual([p["issue"] for p in out], [2, 1])
        self.assertEqual(len(scout.order_passed(passed, 1, 5)), 1)

    def test_issue_less_candidates_group_by_repo(self):
        passed = [cand(None, "x", "mono", f"s{i}", stars=50) for i in range(3)] + [cand(None, "y", "one", None, 40)]
        out = [p["identity"] for p in scout.order_passed(passed, 40, 5)]
        self.assertEqual(out[:2], ["github:x/mono::s0", "github:y/one"])

    def test_duplicate_identities_dropped(self):
        passed = [cand(1, "a", "r", None, 5), cand(2, "a", "r", None, 5)]
        self.assertEqual(len(scout.order_passed(passed, 40, 5)), 1)


class TestIdentityHelpers(unittest.TestCase):
    def queue(self):
        s1, s2 = cand(7, "m", "r", "skills/a", 10), cand(7, "m", "r", "skills/b", 10)
        other = cand(8, "o", "r", None, 3)
        return {"passed": [s1, s2, other], "eligible": [dict(s1, extra="e1"), dict(s2, extra="e2"), other],
                "license_review": [cand(9, "l", "r", None)], "passed_count": 3, "eligible_count": 3}

    def test_remove_by_identity_keeps_siblings(self):
        q, removed = scout.remove_identities_from_queue(self.queue(), ["GITHUB:m/r::skills/a"])
        self.assertEqual(removed, 2)  # passed + eligible
        self.assertEqual([p["identity"] for p in q["passed"]], ["github:m/r::skills/b", "github:o/r"])
        self.assertEqual(q["passed_count"], 2)
        self.assertEqual(q["eligible_count"], 2)
        self.assertEqual(len(q["license_review"]), 1)

    def test_legacy_entry_without_identity(self):
        q = {"passed": [{"issue": 5, "owner": "A", "repo": "B", "subpath": "X/y"}]}
        q2, removed = scout.remove_identities_from_queue(q, ["github:a/b::x/y"])
        self.assertEqual((removed, q2["passed"]), (1, []))

    def test_merge_by_identity_not_issue(self):
        merged = scout.merge_passed_with_eligible(self.queue())
        self.assertEqual([m.get("extra") for m in merged[:2]], ["e1", "e2"])

    def test_remove_published_file_helper_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "candidate-queue-2026-09-26.json"
            path.write_text(json.dumps(self.queue()))
            res = scout.remove_published_from_queue(["github:m/r::skills/b"], path, run_id="hourly-x")
            self.assertEqual(res["removed"], 2)
            data = json.loads(path.read_text())
            self.assertEqual(data["last_publisher_run_id"], "hourly-x")
            self.assertEqual(data["last_removed_identities"], ["github:m/r::skills/b"])
            self.assertEqual(scout.main(["--remove-identity", "github:o/r", "--queue", str(path)]), 0)
            self.assertEqual([p["identity"] for p in json.loads(path.read_text())["passed"]], ["github:m/r::skills/a"])
            missing = scout.remove_published_from_queue(["x"], Path(d) / "nope.json")
            self.assertEqual(missing["removed"], 0)

    def test_assemble_ranks_and_identities(self):
        res = scout.ScoutResult()
        cands = [cand(1, "a", "r", f"s{i}", 100, _skill={}) for i in range(7)] + [cand(2, "b", "r", None, 1, _skill={})]
        res.pass_tier = cands
        scanned = [(c, {"status": "pass", "max_severity": "none"}) for c in cands]
        out = scout.assemble(res, scanned, {}, run_id="t", live_count=0)
        q = out["queue"]
        self.assertEqual(len(q["passed"]), 6)
        self.assertEqual([p["queue_rank"] for p in q["passed"]], list(range(1, 7)))
        self.assertEqual(q["passed"][1]["identity"], "github:b/r")
        self.assertEqual(out["passed_identities"][0], "github:a/r::s0")


class TestImportlibLoading(unittest.TestCase):
    def test_spec_from_file_location_without_registration(self):
        name = "intake_enrich_test_load"
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, str(ROOT / "scripts" / "scout.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # used to crash inside @dataclass
        for fn in ("classify_batch", "run_scan", "rematerialize_warm", "public_cand", "get_gh_token",
                   "fetch_issues_pass", "load_known_holds", "remove_published_from_queue",
                   "merge_passed_with_eligible", "order_passed"):
            self.assertTrue(callable(getattr(mod, fn)), fn)
        sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
