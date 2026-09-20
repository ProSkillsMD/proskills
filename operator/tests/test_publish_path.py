"""Tests for operator publish path: identity, dedupe, dry-run, append-preserve."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from publish_lib import (  # noqa: E402
    build_catalog_identity_set,
    build_skill_record,
    identity_in_catalog,
    parse_github_source,
    stable_id_from_repo,
)
from catalog_update import plan_update  # noqa: E402


def _minimal_catalog(skills: list[dict]) -> dict:
    return {
        "version": "1.1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "total": len(skills),
        "skills": skills,
    }


def _skill(sid: str, repo: str, **extra) -> dict:
    rec = {
        "id": sid,
        "slug": sid,
        "name": sid,
        "category": "other",
        "description": "x",
        "author": "a",
        "author_url": "https://github.com/a",
        "version": "0.0.0",
        "repo_url": repo,
        "works_with": [],
        "paid": False,
        "price": 0,
        "verified_at": "",
        "featured": False,
        "reviewed": False,
        "is_collection": False,
        "scores": {"average": 0},
        "readme": "",
        "files_found": [],
        "github_stars": 0,
    }
    rec.update(extra)
    return rec


class TestIdentityMonorepo(unittest.TestCase):
    def test_root_vs_subpath_distinct(self) -> None:
        root = parse_github_source("https://github.com/Acme/Tools")
        sub_a = parse_github_source("https://github.com/Acme/Tools/tree/main/skills/foo")
        sub_b = parse_github_source("https://github.com/Acme/Tools/tree/main/skills/bar")
        assert root and sub_a and sub_b
        self.assertEqual(root["identity"], "github:acme/tools")
        self.assertEqual(sub_a["identity"], "github:acme/tools::skills/foo")
        self.assertEqual(sub_b["identity"], "github:acme/tools::skills/bar")
        self.assertNotEqual(root["identity"], sub_a["identity"])
        self.assertNotEqual(sub_a["identity"], sub_b["identity"])
        self.assertEqual(root["repo_url"], sub_a["repo_url"])

    def test_stable_id_includes_subpath(self) -> None:
        a = stable_id_from_repo("https://github.com/acme/tools", None)
        b = stable_id_from_repo("https://github.com/acme/tools", "skills/foo")
        self.assertNotEqual(a, b)
        self.assertIn("foo", b)


class TestSkipExistingCatalog(unittest.TestCase):
    def test_skip_existing_root(self) -> None:
        cat = _minimal_catalog(
            [_skill("acme-tools", "https://github.com/acme/tools")]
        )
        identities = build_catalog_identity_set(cat)
        self.assertTrue(identity_in_catalog("github:acme/tools", identities))
        self.assertFalse(identity_in_catalog("github:acme/tools::skills/foo", identities))

    def test_skip_existing_with_skill_path(self) -> None:
        cat = _minimal_catalog(
            [
                _skill(
                    "acme-tools-foo",
                    "https://github.com/acme/tools",
                    skill_path="skills/foo",
                )
            ]
        )
        identities = build_catalog_identity_set(cat)
        self.assertTrue(identity_in_catalog("github:acme/tools::skills/foo", identities))
        self.assertFalse(identity_in_catalog("github:acme/tools", identities))


class TestAppendPreservesExisting(unittest.TestCase):
    def test_append_preserves_ids_slugs_repos(self) -> None:
        existing = [
            _skill("keep-me", "https://github.com/keep/me"),
            _skill("also-keep", "https://github.com/also/keep"),
        ]
        cat = _minimal_catalog(existing)
        candidates = [
            {
                "issue": 99,
                "identity": "github:new/skill",
                "repo_url": "https://github.com/new/skill",
                "subpath": None,
                "owner": "new",
                "repo": "skill",
                "title": "New",
                "stars": 1,
                "source": "test",
                "cache_key": "t",
            }
        ]
        staged, new_skills, skips = plan_update(cat, candidates, offline=True)
        self.assertEqual(len(new_skills), 1)
        self.assertEqual(len(skips), 0)
        self.assertEqual(staged["skills"][0]["id"], "keep-me")
        self.assertEqual(staged["skills"][0]["slug"], "keep-me")
        self.assertEqual(staged["skills"][0]["repo_url"], "https://github.com/keep/me")
        self.assertEqual(staged["skills"][1]["id"], "also-keep")
        self.assertEqual(len(staged["skills"]), 3)
        self.assertEqual(staged["total"], 3)

    def test_skip_candidate_already_in_catalog(self) -> None:
        cat = _minimal_catalog([_skill("new-skill", "https://github.com/new/skill")])
        candidates = [
            {
                "identity": "github:new/skill",
                "repo_url": "https://github.com/new/skill",
                "subpath": None,
                "owner": "new",
                "repo": "skill",
            }
        ]
        staged, new_skills, skips = plan_update(cat, candidates, offline=True)
        self.assertEqual(new_skills, [])
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["reason"], "already_in_catalog")
        self.assertEqual(len(staged["skills"]), 1)

    def test_monorepo_subpath_appends_alongside_root(self) -> None:
        cat = _minimal_catalog([_skill("acme-tools", "https://github.com/acme/tools")])
        candidates = [
            {
                "identity": "github:acme/tools::skills/foo",
                "repo_url": "https://github.com/acme/tools",
                "subpath": "skills/foo",
                "owner": "acme",
                "repo": "tools",
            }
        ]
        staged, new_skills, skips = plan_update(cat, candidates, offline=True)
        self.assertEqual(len(skips), 0)
        self.assertEqual(len(new_skills), 1)
        self.assertEqual(new_skills[0].get("skill_path"), "skills/foo")
        self.assertEqual(staged["skills"][0]["id"], "acme-tools")


class TestDryRunNoWrite(unittest.TestCase):
    def test_catalog_update_dry_run_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            catalog_path = tmp_path / "skills-catalog.json"
            cand_path = tmp_path / "candidates.json"
            out_path = tmp_path / "skills-catalog.staged.json"
            delta_path = tmp_path / "delta.json"
            catalog_path.write_text(
                json.dumps(_minimal_catalog([_skill("keep-me", "https://github.com/keep/me")])),
                encoding="utf-8",
            )
            cand_path.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "identity": "github:new/skill",
                                "repo_url": "https://github.com/new/skill",
                                "subpath": None,
                                "owner": "new",
                                "repo": "skill",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "catalog_update.py"),
                    "--candidates",
                    str(cand_path),
                    "--catalog",
                    str(catalog_path),
                    "--out",
                    str(out_path),
                    "--delta-out",
                    str(delta_path),
                    "--offline",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertFalse(out_path.exists())
            self.assertFalse(delta_path.exists())
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["stage"], "catalog_update")

    def test_discover_help(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "discover.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--limit", proc.stdout)
        self.assertIn("--from-issues", proc.stdout)

    def test_discover_dry_run_with_local_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            catalog_path = tmp_path / "skills-catalog.json"
            issues_path = tmp_path / "issues.json"
            catalog_path.write_text(
                json.dumps(_minimal_catalog([_skill("keep-me", "https://github.com/keep/me")])),
                encoding="utf-8",
            )
            issues_path.write_text(
                json.dumps(
                    [
                        {
                            "number": 100,
                            "title": "Add https://github.com/brand-new/skill",
                            "body": "Please add https://github.com/brand-new/skill",
                            "updated_at": "2026-09-01T00:00:00Z",
                            "labels": [{"name": "submission"}],
                        },
                        {
                            "number": 714,
                            "title": "Protected https://github.com/should/skip",
                            "body": "https://github.com/should/skip",
                            "updated_at": "2026-09-01T00:00:00Z",
                            "labels": [],
                        },
                        {
                            "number": 101,
                            "title": "Dup https://github.com/keep/me",
                            "body": "already published https://github.com/keep/me",
                            "updated_at": "2026-09-01T00:00:00Z",
                            "labels": [],
                        },
                    ]
                ),
                encoding="utf-8",
            )
            out = tmp_path / "out.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "discover.py"),
                    "--catalog",
                    str(catalog_path),
                    "--issues-json",
                    str(issues_path),
                    "--output",
                    str(out),
                    "--limit",
                    "10",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertFalse(out.exists())
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["plan"]["candidate_count"], 1)


class TestBuildSkillRecord(unittest.TestCase):
    def test_record_shape(self) -> None:
        rec = build_skill_record(
            owner="acme",
            repo="tools",
            repo_url="https://github.com/acme/tools",
            subpath="skills/foo",
            skill_md="---\nname: Foo Skill\ndescription: Hello\ncategory: ai\n---\n\nBody\n",
            readme="# Readme\n",
            stars=3,
        )
        self.assertEqual(rec["name"], "Foo Skill")
        self.assertEqual(rec["category"], "ai")
        self.assertEqual(rec["skill_path"], "skills/foo")
        self.assertEqual(rec["repo_url"], "https://github.com/acme/tools")
        self.assertIn("id", rec)
        self.assertIn("slug", rec)
        self.assertIn("scores", rec)


if __name__ == "__main__":
    unittest.main()
