"""catalog_update.py handling of subfolder identities (github:owner/repo::subpath). Offline."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog_update  # noqa: E402
from catalog_update import added_identity_map, plan_update  # noqa: E402


def cat(skills):
    return {"version": "1.1.0", "skills": skills, "total": len(skills)}


def sub_cand(sub, issue=7, **kw):
    c = {"issue": issue, "identity": f"github:acme/tools::{sub.lower()}", "owner": "acme", "repo": "tools",
         "repo_url": "https://github.com/acme/tools", "subpath": sub.lower(), "skill_path": f"{sub}/SKILL.md",
         "default_branch": "dev", "stars": 3}
    c.update(kw)
    return c


class TestSubfolderStaging(unittest.TestCase):
    def test_unique_ids_source_url_and_map(self):
        staged, new, skips = plan_update(cat([]), [sub_cand("skills/Foo"), sub_cand("skills/bar", issue=8)], offline=True)
        self.assertEqual(skips, [])
        self.assertEqual([s["id"] for s in new], ["acme-tools-skills-foo", "acme-tools-skills-bar"])
        self.assertEqual(new[0]["repo_url"], "https://github.com/acme/tools")
        self.assertEqual(new[0]["skill_path"], "skills/Foo")  # original case keeps links valid
        self.assertEqual(new[0]["source_url"], "https://github.com/acme/tools/tree/dev/skills/Foo")
        amap = added_identity_map(new)
        self.assertEqual([(a["identity"], a["issue"], a["slug"]) for a in amap],
                         [("github:acme/tools::skills/foo", 7, "acme-tools-skills-foo"),
                          ("github:acme/tools::skills/bar", 8, "acme-tools-skills-bar")])

    def test_id_collision_suffix_and_existing_subpath_skip(self):
        existing = [{"id": "acme-tools-skills-foo", "slug": "acme-tools-skills-foo",
                     "repo_url": "https://github.com/other/x"},
                    {"id": "e2", "slug": "e2", "repo_url": "https://github.com/acme/tools", "skill_path": "skills/bar"}]
        staged, new, skips = plan_update(cat(existing), [sub_cand("skills/foo"), sub_cand("skills/bar")], offline=True)
        self.assertEqual([s["id"] for s in new], ["acme-tools-skills-foo-2"])
        self.assertEqual(skips[0]["reason"], "already_in_catalog")

    def test_root_skill_has_no_source_url(self):
        c = {"issue": 1, "identity": "github:acme/root", "owner": "acme", "repo": "root",
             "repo_url": "https://github.com/acme/root", "subpath": None, "skill_path": "SKILL.md"}
        _, new, _ = plan_update(cat([]), [c], offline=True)
        self.assertNotIn("source_url", new[0])
        self.assertNotIn("skill_path", new[0])

    def test_fetch_uses_default_branch_and_original_case_paths(self):
        calls = []

        def fake_fetch(owner, repo, path, refs=("main", "master")):
            calls.append((path, refs))
            return "---\nname: foo\n---\nbody" if path.endswith("SKILL.md") else None
        with mock.patch.object(catalog_update, "fetch_raw_text", fake_fetch):
            _, new, _ = plan_update(cat([]), [sub_cand("skills/Foo")])
        self.assertEqual(calls[0], ("skills/Foo/SKILL.md", ("dev", "main", "master")))
        self.assertEqual(calls[1][0], "skills/Foo/README.md")  # skill README first
        self.assertEqual(calls[2][0], "README.md")
        self.assertEqual(new[0]["name"], "foo")


if __name__ == "__main__":
    unittest.main()
