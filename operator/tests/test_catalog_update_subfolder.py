"""catalog_update.py handling of subfolder identities (github:owner/repo::subpath). Offline."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog_update  # noqa: E402
from catalog_update import added_identity_map, plan_update, source_tree_url  # noqa: E402
from publish_lib import fetch_raw_text, quote_github_path  # noqa: E402

# Issue #10042: this folder name is not ASCII and used to crash the raw fetch.
ISSUE_PATH = "hua695/mathmodeling-skills-arsenal/skills/03-writing/论文辅助"
ENCODED_ISSUE_PATH = "hua695/mathmodeling-skills-arsenal/skills/03-writing/%E8%AE%BA%E6%96%87%E8%BE%85%E5%8A%A9"


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

    def test_chinese_subfolder_source_url_is_percent_encoded(self):
        folder = "skills/03-writing/论文辅助"
        _, new, skips = plan_update(cat([]), [sub_cand(folder)], offline=True)
        self.assertEqual(skips, [])
        self.assertEqual(
            new[0]["source_url"],
            "https://github.com/acme/tools/tree/dev/skills/03-writing/%E8%AE%BA%E6%96%87%E8%BE%85%E5%8A%A9",
        )
        self.assertTrue(new[0]["source_url"].isascii())


class TestGithubPathEncoding(unittest.TestCase):
    def test_ascii_paths_stay_byte_identical(self):
        samples = [
            "skills/03-writing/SKILL.md",
            "hua695/mathmodeling-skills-arsenal/skills/03-writing/SKILL.md",
            "skills/my skill/SKILL.md",
        ]
        for sample in samples:
            self.assertEqual(quote_github_path(sample), sample)
        self.assertEqual(
            source_tree_url("acme", "tools", "skills/Foo", "dev"),
            "https://github.com/acme/tools/tree/dev/skills/Foo",
        )

    def test_issue_10042_chinese_path_is_percent_encoded(self):
        self.assertEqual(quote_github_path(ISSUE_PATH), ENCODED_ISSUE_PATH)
        self.assertEqual(
            source_tree_url("hua695", "mathmodeling-skills-arsenal", "skills/03-writing/论文辅助", "main"),
            "https://github.com/hua695/mathmodeling-skills-arsenal/tree/main/"
            "skills/03-writing/%E8%AE%BA%E6%96%87%E8%BE%85%E5%8A%A9",
        )
        seen: list[str] = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return "正文".encode()

        def fake_open(req, timeout=20):
            url = req.full_url
            url.encode("ascii")
            seen.append(url)
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_open):
            text = fetch_raw_text(
                "hua695", "mathmodeling-skills-arsenal", "skills/03-writing/论文辅助", refs=("main",),
            )
        self.assertEqual(text, "正文")
        self.assertEqual(
            seen,
            ["https://raw.githubusercontent.com/hua695/mathmodeling-skills-arsenal/main/"
             "skills/03-writing/%E8%AE%BA%E6%96%87%E8%BE%85%E5%8A%A9"],
        )

    def test_ascii_raw_url_is_byte_identical(self):
        seen: list[str] = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ok"

        def fake_open(req, timeout=20):
            seen.append(req.full_url)
            return Resp()

        path = "skills/03-writing/SKILL.md"
        with mock.patch("urllib.request.urlopen", fake_open):
            text = fetch_raw_text("hua695", "mathmodeling-skills-arsenal", path, refs=("main",))
        self.assertEqual(text, "ok")
        self.assertEqual(
            seen,
            [f"https://raw.githubusercontent.com/hua695/mathmodeling-skills-arsenal/main/{path}"],
        )


if __name__ == "__main__":
    unittest.main()
