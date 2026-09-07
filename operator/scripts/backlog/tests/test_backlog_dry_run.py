import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_backlog_dry_run import build  # noqa: E402
from normalize import normalize_repo_url, is_malformed_url  # noqa: E402


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

    def test_monorepo_subpath_collapses_to_owner_repo(self):
        self.assertEqual(
            normalize_repo_url("https://github.com/owner/repo/tree/main/skills/foo"),
            "https://github.com/owner/repo",
        )
        self.assertEqual(
            normalize_repo_url("https://github.com/owner/repo/blob/main/README.md"),
            "https://github.com/owner/repo",
        )

    def test_distinct_repos_not_merged(self):
        a = normalize_repo_url("https://github.com/owner/repo-a")
        b = normalize_repo_url("https://github.com/owner/repo-b")
        self.assertNotEqual(a, b)

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


class BacklogDryRunTests(unittest.TestCase):
    def test_dispositions(self):
        issues = {
            "issues": [
                {"number": 1, "title": "[submission] a", "body": "https://github.com/o/a", "createdAt": "2026-01-01T00:00:00Z"},
                {"number": 2, "title": "[submission] a dup", "body": "https://github.com/o/a", "createdAt": "2026-01-02T00:00:00Z"},
                {"number": 3, "title": "[submission] published", "body": "https://github.com/o/pub", "createdAt": "2026-01-01T00:00:00Z"},
                {"number": 4, "title": "[submission] none", "body": "no url", "createdAt": "2026-01-01T00:00:00Z"},
                {"number": 5, "title": "[submission] bad", "body": "https://github.com/onlyowner", "createdAt": "2026-01-01T00:00:00Z"},
            ]
        }
        catalog = {"skills": [{"repo_url": "https://github.com/o/pub"}]}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps(issues))
            (td / "cat.json").write_text(json.dumps(catalog))
            out = td / "out"
            summary = build(td / "issues.json", td / "cat.json", out)
            self.assertEqual(summary["total"], 5)
            self.assertEqual(summary["counts"].get("canonical_actionable"), 1)
            self.assertEqual(summary["counts"].get("normalized_source_duplicate"), 1)
            self.assertEqual(summary["counts"].get("already_published"), 1)
            self.assertEqual(summary["counts"].get("missing_source"), 1)
            self.assertEqual(summary["counts"].get("invalid_source"), 1)
            exec_m = json.loads((out / "backlog-execution-manifest.json").read_text())
            self.assertIn("issue_numbers_by_disposition", exec_m)
            self.assertEqual(exec_m["issue_numbers_by_disposition"]["canonical_actionable"], [1])
            rb = json.loads((out / "backlog-rollback-manifest.json").read_text())
            self.assertEqual(rb["issue_numbers_touched"], [])

    def test_idempotent_except_timestamp(self):
        issues = {
            "issues": [
                {"number": 1, "title": "a", "body": "https://github.com/o/a", "createdAt": "2026-01-01T00:00:00Z"},
            ]
        }
        catalog = {"skills": []}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps(issues))
            (td / "cat.json").write_text(json.dumps(catalog))
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
