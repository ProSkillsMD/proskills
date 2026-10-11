"""Tests for catalog review queue / gating / apply invariants."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_review  # noqa: E402
import catalog_review as CR  # noqa: E402
import catalog_review_apply as CA  # noqa: E402
import catalog_review_queue as CQ  # noqa: E402


def _scores(**overrides: float) -> dict[str, float]:
    base = {k: 7.0 for k in ai_review.WEIGHTS}
    base.update(overrides)
    return base


class TestWeightedAverage(unittest.TestCase):
    def test_matches_gamora_weights_one_decimal(self) -> None:
        sc = {
            "functionality": 8.0,
            "documentation": 6.0,
            "security": 8.0,
            "maintenance": 6.0,
            "usefulness": 8.0,
            "uniqueness": 6.0,
            "code_quality": 6.0,
        }
        # weights sum = 8.5; num = 60; 60/8.5 ≈ 7.0588 -> 7.1 (1 dp) / 7.06 (2 dp)
        self.assertEqual(CR.weighted_average(sc), 7.1)
        self.assertEqual(ai_review.weighted(sc), 7.06)

    def test_threshold_boundary(self) -> None:
        sc = {k: 6.0 for k in ai_review.WEIGHTS}
        self.assertEqual(CR.weighted_average(sc), 6.0)
        self.assertTrue(CR.gating_pass(s1_ok=True, verdict="pass", weighted=6.0))
        self.assertFalse(CR.gating_pass(s1_ok=True, verdict="pass", weighted=5.9))


class TestGatingRule(unittest.TestCase):
    def test_requires_s1_and_ai_pass_and_threshold(self) -> None:
        self.assertTrue(CR.gating_pass(s1_ok=True, verdict="pass", weighted=7.0))
        self.assertFalse(CR.gating_pass(s1_ok=False, verdict="pass", weighted=9.0))
        self.assertFalse(CR.gating_pass(s1_ok=True, verdict="hold", weighted=9.0))
        self.assertFalse(CR.gating_pass(s1_ok=True, verdict="pass", weighted=5.99))


class TestQueueRank(unittest.TestCase):
    def test_stars_then_activity(self) -> None:
        rows = [
            {"github_stars": 10, "activity_at": "2026-01-01", "slug": "a"},
            {"github_stars": 10, "activity_at": "2026-06-01", "slug": "b"},
            {"github_stars": 50, "activity_at": "", "slug": "c"},
            {"github_stars": 10, "activity_at": "", "slug": "d"},
        ]
        got = [r["slug"] for r in CQ.rank_by_priority(rows)]
        self.assertEqual(got, ["c", "b", "a", "d"])


class TestApplyInvariants(unittest.TestCase):
    def _catalog(self, n: int = 3) -> dict:
        skills = []
        for i in range(n):
            skills.append({
                "id": f"skill-{i}",
                "slug": f"skill-{i}",
                "name": f"Skill {i}",
                "category": "other",
                "repo_url": f"https://github.com/ex/repo-{i}",
                "reviewed": False,
                "verified_at": "",
                "scores": {**{k: 0 for k in ai_review.WEIGHTS}, "average": 0},
                "description": "d",
            })
        return {"version": "1.1.0", "total": n, "skills": skills}

    def test_patch_only_target_rows_no_add_remove(self) -> None:
        cat = self._catalog(3)
        results = [{
            "id": "skill-1",
            "slug": "skill-1",
            "reviewed": True,
            "verdict": "pass",
            "weighted": 7.2,
            "scores": {**_scores(), "average": 7.2},
            "checks": {"license": {"status": "pass", "detail": "MIT"}},
            "reasons": ["solid skill"],
            "judged_by": "agent",
            "sha": "abc",
        }]
        staged, log = CA.plan_patches(cat, results, verified_at="2026-10-08")
        self.assertEqual(len(staged["skills"]), 3)
        self.assertEqual(staged["total"], 3)
        self.assertEqual(len(log), 1)
        self.assertTrue(staged["skills"][1]["reviewed"])
        self.assertFalse(staged["skills"][0]["reviewed"])
        self.assertFalse(staged["skills"][2]["reviewed"])
        self.assertEqual(staged["skills"][1]["review_version"], "catalog-review/1")
        self.assertEqual(staged["skills"][1]["verified_at"], "2026-10-08")
        # identity unchanged
        for i in range(3):
            self.assertEqual(staged["skills"][i]["id"], cat["skills"][i]["id"])
            self.assertEqual(staged["skills"][i]["slug"], cat["skills"][i]["slug"])
            self.assertEqual(staged["skills"][i]["repo_url"], cat["skills"][i]["repo_url"])
            self.assertEqual(staged["skills"][i]["category"], cat["skills"][i]["category"])
        CA.assert_apply_invariants(cat, staged, {"skill-1"})

    def test_fail_stays_reviewed_false(self) -> None:
        cat = self._catalog(1)
        results = [{
            "id": "skill-0",
            "slug": "skill-0",
            "reviewed": False,
            "verdict": "fail",
            "weighted": None,
            "scores": None,
            "checks": {"license": {"status": "fail", "detail": "no SPDX"}},
            "reasons": ["license:reject"],
            "judged_by": None,
            "critical": False,
        }]
        staged, _ = CA.plan_patches(cat, results, verified_at="2026-10-08")
        self.assertFalse(staged["skills"][0]["reviewed"])
        self.assertEqual(staged["skills"][0]["verified_at"], "")  # unchanged
        self.assertEqual(staged["skills"][0]["review_evidence"]["verdict"], "fail")
        self.assertEqual(len(staged["skills"]), 1)

    def test_refuse_unknown_id(self) -> None:
        cat = self._catalog(1)
        with self.assertRaises(ValueError):
            CA.plan_patches(cat, [{"id": "missing", "slug": "missing", "reviewed": True,
                                   "scores": {**_scores(), "average": 7.0}, "checks": {},
                                   "reasons": [], "judged_by": "agent"}], verified_at="2026-10-08")

    def test_non_target_row_change_detected(self) -> None:
        cat = self._catalog(2)
        staged = deepcopy(cat)
        staged["skills"][0]["name"] = "MUTATED"
        with self.assertRaises(AssertionError):
            CA.assert_apply_invariants(cat, staged, {"skill-1"})


class TestS1Offline(unittest.TestCase):
    def test_missing_skill_md_fails(self) -> None:
        skill = {"id": "x", "slug": "x", "name": "X", "description": "Y", "repo_url": "https://github.com/a/b"}
        r = CR.run_s1_on_skill(skill, offline=True)
        self.assertFalse(r["s1_ok"])
        self.assertIn("missing SKILL.md content", r["reasons"])

    def test_clean_skill_with_spdx_passes(self) -> None:
        md = "---\nname: demo\ndescription: A concrete demo skill for agents.\n---\n\n# Demo\nDo a thing.\n"
        skill = {
            "id": "demo", "slug": "demo", "name": "demo", "description": "A concrete demo skill for agents.",
            "repo_url": "https://github.com/a/b", "skill_md": md, "license_spdx": "MIT",
        }
        r = CR.run_s1_on_skill(skill, offline=True)
        self.assertTrue(r["s1_ok"], r)
        self.assertFalse(r["critical"])


if __name__ == "__main__":
    unittest.main()
