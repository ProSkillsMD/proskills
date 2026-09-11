import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_backlog_dry_run import build, extract_all_candidates  # noqa: E402
from normalize import (  # noqa: E402
    normalize_repo_url,
    normalize_source_identity,
    is_malformed_url,
)


class NormalizeTests(unittest.TestCase):
    def test_strip_query_fragment_slash_git(self):
        self.assertEqual(
            normalize_repo_url("https://github.com/Owner/Repo.git?utm=1#frag"),
            "https://github.com/owner/repo",
        )
        self.assertEqual(
            normalize_repo_url("https://github.com/owner/repo/"),
            "https://github.com/owner/repo",
        )

    def test_monorepo_subpath_preserved_as_secondary_identity(self):
        ident = normalize_source_identity(
            "https://github.com/owner/repo/tree/main/skills/foo"
        )
        self.assertEqual(ident["repo"], "https://github.com/owner/repo")
        self.assertEqual(ident["subpath"], "skills/foo")
        self.assertEqual(ident["identity_key"], "https://github.com/owner/repo::skills/foo")
        # repo-only helper still returns owner/repo
        self.assertEqual(
            normalize_repo_url("https://github.com/owner/repo/tree/main/skills/foo"),
            "https://github.com/owner/repo",
        )
        blob = normalize_source_identity(
            "https://github.com/owner/repo/blob/main/skills/bar/SKILL.md"
        )
        self.assertEqual(blob["subpath"], "skills/bar/skill.md")

    def test_distinct_repos_not_merged(self):
        a = normalize_repo_url("https://github.com/owner/repo-a")
        b = normalize_repo_url("https://github.com/owner/repo-b")
        self.assertNotEqual(a, b)

    def test_distinct_subpaths_not_same_identity(self):
        a = normalize_source_identity(
            "https://github.com/owner/mono/tree/main/skills/a"
        )
        b = normalize_source_identity(
            "https://github.com/owner/mono/tree/main/skills/b"
        )
        self.assertEqual(a["repo"], b["repo"])
        self.assertNotEqual(a["identity_key"], b["identity_key"])

    def test_root_vs_explicit_subpath_distinct(self):
        root = normalize_source_identity("https://github.com/owner/mono")
        sub = normalize_source_identity(
            "https://github.com/owner/mono/tree/main/skills/a"
        )
        self.assertNotEqual(root["identity_key"], sub["identity_key"])

    def test_ssh_and_shorthand(self):
        self.assertEqual(
            normalize_repo_url("git@github.com:Owner/Repo.git"),
            "https://github.com/owner/repo",
        )
        self.assertEqual(
            normalize_repo_url("Owner/Repo"),
            "https://github.com/owner/repo",
        )

    def test_malformed(self):
        self.assertTrue(is_malformed_url("https://github.com/onlyowner"))
        self.assertIsNone(normalize_repo_url("not a url"))


class CandidateExtractionTests(unittest.TestCase):
    def test_multiple_equivalent_urls(self):
        body = (
            "https://github.com/Owner/Repo\n"
            "https://github.com/owner/repo.git\n"
            "https://GITHUB.com/owner/repo/"
        )
        cands = extract_all_candidates("t", body)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["repo"], "https://github.com/owner/repo")

    def test_multiple_competing_urls(self):
        body = (
            "https://github.com/owner/repo-a\n"
            "https://github.com/owner/repo-b"
        )
        cands = extract_all_candidates("t", body)
        self.assertEqual(len(cands), 2)


class BacklogDryRunTests(unittest.TestCase):
    def _run(self, issues, catalog):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps({"issues": issues}))
            (td / "cat.json").write_text(json.dumps({"skills": catalog}))
            out = td / "out"
            summary = build(td / "issues.json", td / "cat.json", out)
            manifest = json.loads((out / "backlog-cleanup-manifest.json").read_text())
            return summary, manifest, out

    def test_basic_dispositions(self):
        issues = [
            {"number": 1, "title": "[submission] a", "body": "https://github.com/o/a", "createdAt": "2026-01-01T00:00:00Z"},
            {"number": 2, "title": "[submission] a dup", "body": "https://github.com/o/a", "createdAt": "2026-01-02T00:00:00Z"},
            {"number": 3, "title": "[submission] published", "body": "https://github.com/o/pub", "createdAt": "2026-01-01T00:00:00Z"},
            {"number": 4, "title": "[submission] none", "body": "no url", "createdAt": "2026-01-01T00:00:00Z"},
            {"number": 5, "title": "[submission] bad", "body": "https://github.com/onlyowner", "createdAt": "2026-01-01T00:00:00Z"},
        ]
        catalog = [{"id": "pub", "repo_url": "https://github.com/o/pub"}]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps({"issues": issues}))
            (td / "cat.json").write_text(json.dumps({"skills": catalog}))
            out = td / "out"
            summary = build(td / "issues.json", td / "cat.json", out)
            self.assertEqual(summary["total"], 5)
            self.assertEqual(summary["counts"].get("canonical_actionable"), 1)
            self.assertEqual(summary["counts"].get("normalized_source_duplicate"), 1)
            self.assertEqual(summary["counts"].get("already_published"), 1)
            self.assertEqual(summary["counts"].get("missing_source"), 1)
            self.assertEqual(summary["counts"].get("invalid_source"), 1)
            exec_m = json.loads((out / "backlog-execution-manifest.json").read_text())
            self.assertEqual(exec_m["issue_numbers_by_disposition"]["canonical_actionable"], [1])
            rb = json.loads((out / "backlog-rollback-manifest.json").read_text())
            self.assertEqual(rb["issue_numbers_touched"], [])

    def test_two_skills_different_folders_same_monorepo(self):
        issues = [
            {
                "number": 10,
                "title": "skill a",
                "body": "https://github.com/acme/mono/tree/main/skills/alpha",
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 11,
                "title": "skill b",
                "body": "https://github.com/acme/mono/tree/main/skills/beta",
                "createdAt": "2026-01-02T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, [])
        by_n = {i["number"]: i for i in manifest["items"]}
        self.assertEqual(by_n[10]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[11]["disposition"], "canonical_actionable")
        self.assertNotEqual(by_n[10]["subpath"], by_n[11]["subpath"])

    def test_root_repo_versus_explicit_subpath(self):
        issues = [
            {
                "number": 20,
                "title": "root",
                "body": "https://github.com/acme/mono",
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 21,
                "title": "sub",
                "body": "https://github.com/acme/mono/tree/main/skills/alpha",
                "createdAt": "2026-01-02T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, [])
        by_n = {i["number"]: i for i in manifest["items"]}
        self.assertEqual(by_n[20]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[21]["disposition"], "canonical_actionable")
        self.assertIsNone(by_n[20]["subpath"])
        self.assertEqual(by_n[21]["subpath"], "skills/alpha")

    def test_multiple_equivalent_urls_classify_normally(self):
        issues = [
            {
                "number": 30,
                "title": "eq",
                "body": (
                    "see https://github.com/Owner/Repo and "
                    "https://github.com/owner/repo.git"
                ),
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 31,
                "title": "eq dup",
                "body": "https://github.com/owner/repo",
                "createdAt": "2026-01-02T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, [])
        by_n = {i["number"]: i for i in manifest["items"]}
        self.assertEqual(by_n[30]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[31]["disposition"], "normalized_source_duplicate")
        self.assertEqual(by_n[31]["canonical_number"], 30)

    def test_multiple_competing_urls_ambiguous(self):
        issues = [
            {
                "number": 40,
                "title": "compete",
                "body": (
                    "https://github.com/owner/repo-a\n"
                    "https://github.com/owner/repo-b"
                ),
                "createdAt": "2026-01-01T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, [])
        item = manifest["items"][0]
        self.assertEqual(item["disposition"], "ambiguous_manual_review")
        self.assertEqual(len(item["candidates"]), 2)
        # no body / PII fields
        self.assertNotIn("body", item)
        self.assertNotIn("title", item)

    def test_published_matching_and_nonmatching_subpaths(self):
        catalog = [{"id": "pub-root", "repo_url": "https://github.com/acme/published"}]
        issues = [
            {
                "number": 50,
                "title": "match root",
                "body": "https://github.com/acme/published",
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 51,
                "title": "nonmatching subpath",
                "body": "https://github.com/acme/published/tree/main/skills/other",
                "createdAt": "2026-01-02T00:00:00Z",
            },
            {
                "number": 52,
                "title": "another already published",
                "body": "https://github.com/acme/published.git",
                "createdAt": "2026-01-03T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, catalog)
        by_n = {i["number"]: i for i in manifest["items"]}
        self.assertEqual(by_n[50]["disposition"], "already_published")
        self.assertEqual(by_n[50]["published_skill_ids"], ["pub-root"])
        # distinct explicit subpath must not be absorbed by root catalog record
        self.assertEqual(by_n[51]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[51]["subpath"], "skills/other")
        # multiple issues may be already_published for same skill
        self.assertEqual(by_n[52]["disposition"], "already_published")
        self.assertEqual(by_n[52]["published_skill_ids"], ["pub-root"])

    def test_deterministic_canonical_selection(self):
        issues = [
            {
                "number": 62,
                "title": "later number earlier time wins? no — time first",
                "body": "https://github.com/o/x",
                "createdAt": "2026-01-02T00:00:00Z",
            },
            {
                "number": 61,
                "title": "earlier",
                "body": "https://github.com/o/x",
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 60,
                "title": "same time lower number",
                "body": "https://github.com/o/y",
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 63,
                "title": "same time higher number",
                "body": "https://github.com/o/y",
                "createdAt": "2026-01-01T00:00:00Z",
            },
        ]
        summary, manifest, _ = self._run(issues, [])
        by_n = {i["number"]: i for i in manifest["items"]}
        self.assertEqual(by_n[61]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[62]["disposition"], "normalized_source_duplicate")
        self.assertEqual(by_n[62]["canonical_number"], 61)
        self.assertEqual(by_n[60]["disposition"], "canonical_actionable")
        self.assertEqual(by_n[63]["disposition"], "normalized_source_duplicate")
        self.assertEqual(by_n[63]["canonical_number"], 60)

    def test_idempotent_except_timestamp(self):
        issues = [
            {"number": 1, "title": "a", "body": "https://github.com/o/a", "createdAt": "2026-01-01T00:00:00Z"},
        ]
        catalog = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps({"issues": issues}))
            (td / "cat.json").write_text(json.dumps({"skills": catalog}))
            out1, out2 = td / "o1", td / "o2"
            build(td / "issues.json", td / "cat.json", out1)
            build(td / "issues.json", td / "cat.json", out2)

            def strip(obj):
                if isinstance(obj, dict):
                    return {k: strip(v) for k, v in obj.items() if k != "generated_at"}
                if isinstance(obj, list):
                    return [strip(x) for x in obj]
                return obj

            for name in (
                "backlog-cleanup-summary.json",
                "backlog-cleanup-manifest.json",
                "backlog-execution-manifest.json",
                "backlog-rollback-manifest.json",
            ):
                a = json.loads((out1 / name).read_text())
                b = json.loads((out2 / name).read_text())
                self.assertEqual(strip(a), strip(b), name)
            self.assertEqual(
                (out1 / "backlog-cleanup-summary.csv").read_bytes(),
                (out2 / "backlog-cleanup-summary.csv").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
