import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_backlog_dry_run import build  # noqa: E402


class BacklogDryRunTests(unittest.TestCase):
    def test_dispositions(self):
        issues = {
            "issues": [
                {"number": 1, "title": "[submission] a", "body": "https://github.com/o/a", "createdAt": "2026-01-01T00:00:00Z"},
                {"number": 2, "title": "[submission] a dup", "body": "https://github.com/o/a", "createdAt": "2026-01-02T00:00:00Z"},
                {"number": 3, "title": "[submission] published", "body": "https://github.com/o/pub", "createdAt": "2026-01-01T00:00:00Z"},
                {"number": 4, "title": "[submission] none", "body": "no url", "createdAt": "2026-01-01T00:00:00Z"},
            ]
        }
        catalog = {"skills": [{"repo_url": "https://github.com/o/pub"}]}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "issues.json").write_text(json.dumps(issues))
            (td / "cat.json").write_text(json.dumps(catalog))
            out = td / "out"
            summary = build(td / "issues.json", td / "cat.json", out)
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["counts"].get("canonical_actionable"), 1)
            self.assertEqual(summary["counts"].get("normalized_source_duplicate"), 1)
            self.assertEqual(summary["counts"].get("already_published"), 1)
            self.assertEqual(summary["counts"].get("missing_source"), 1)


if __name__ == "__main__":
    unittest.main()
