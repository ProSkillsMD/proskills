"""Owner content policy: adult/NSFW candidates are held in review and never selected by publish."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import content_policy as CP  # noqa: E402
import issue_flow as F  # noqa: E402
import publish_candidates as PC  # noqa: E402
from test_issue_flow import Base, src_rec  # noqa: E402

NSFW_SKILL = ("---\nname: qwen-nsfw-pose\ndescription: Generate sexy NSFW posed images with pose LoRAs.\n"
              "license: MIT\n---\n\n# Pose\n\nUse it.\n")


class TestMatcher(unittest.TestCase):
    def test_hits(self):
        for t in ("qwen21-nsfw-pose", "Compile adult NSFW image prompts", "hentai art", "erotic stories",
                  "generate nude images", "porn"):
            self.assertTrue(CP.violates(t), t)

    def test_false_positives(self):
        for t in ("sexagesimal converter", "Sextant principles", "Essex council data", "sexual-health check-ins",
                  "special category data: sexual orientation", "NSFW detection guardrail", "filter nsfw content",
                  "authorized adult presenter image", "explicit approval only", "node 18+"):
            self.assertFalse(CP.violates(t), t)

    def test_label_registered(self):
        self.assertIn(CP.LABEL, F.LABELS)


class TestPolicyFlow(Base):
    def test_nsfw_held_in_review_and_not_selected(self):
        self.gh.add_repo("N/nsfw-pose", {"s/SKILL.md": NSFW_SKILL}, stars=500)
        out, _ = self.file([src_rec("N", "nsfw-pose", "s", stars=500)])
        n = out["actions"][0]["issue"]
        self.reviewer().run(list(self.gh.issues.values()))
        labs = self.gh.labels_of(n)
        self.assertIn(CP.LABEL, labs)
        self.assertNotIn("review:pass", labs)
        # even if someone forces review:pass, publish selection refuses it
        self.gh.issues[n]["labels"] = [{"name": l} for l in ("candidate", "review:pass")]
        sel = PC.select(self.api, self.state, catalog={"skills": []}, issues=list(self.gh.issues.values()))
        self.assertEqual(sel["passed"], [])
        self.assertEqual(sel["skipped"][0]["reason"], "content_policy")
        self.gh.issues[n]["labels"].append({"name": CP.LABEL})
        sel = PC.select(self.api, self.state, catalog={"skills": []}, issues=list(self.gh.issues.values()))
        self.assertEqual(sel["skipped"][0]["reason"], "held_label")


if __name__ == "__main__":
    unittest.main()
