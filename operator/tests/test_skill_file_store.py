"""Per-skill file layout: index in the catalog, inline text beside it."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import skill_file_store as S  # noqa: E402


def _skill(content: str = "hello\n") -> dict:
    return {
        "id": "getsentry",
        "slug": "getsentry",
        "version": "1.8.0",
        "files": [
            {"path": "SKILL.md", "size": len(content.encode()), "sha": "abc", "content": content},
            {"path": "logo.png", "size": 4, "sha": "def", "omitted": True},
        ],
    }


class TestSkillFileStore(unittest.TestCase):
    def test_index_drops_content_and_keeps_flags(self):
        indexed = S.index_skill(_skill())
        self.assertEqual(indexed["version"], "1.8.0")
        self.assertEqual(indexed["files"][0], {"path": "SKILL.md", "size": 6, "sha": "abc"})
        self.assertEqual(indexed["files"][1], {"path": "logo.png", "size": 4, "sha": "def", "omitted": True})
        self.assertIs(S.index_skill(indexed), indexed)

    def test_document_schema_and_roundtrip_are_idempotent(self):
        doc = S.content_document(_skill())
        self.assertEqual(doc["schema"], S.SCHEMA)
        self.assertEqual(doc["slug"], "getsentry")
        self.assertEqual(doc["id"], "getsentry")
        self.assertEqual([item["path"] for item in doc["files"]], ["SKILL.md"])
        self.assertNotIn("omitted", doc["files"][0])
        with tempfile.TemporaryDirectory() as tmp:
            website = Path(tmp)
            catalog = {"version": "1.1.0", "skills": [_skill(), {"id": "plain", "slug": "plain"}]}
            first = S.write_website_catalog(website, catalog)
            text = (website / "public/skill-files/getsentry.json").read_text(encoding="utf-8")
            catalog_text = (website / S.CATALOG_REL).read_text(encoding="utf-8")
            self.assertNotIn('"content"', catalog_text)
            self.assertIn("hello\\n", text)
            second = S.write_website_catalog(website, first)
            self.assertEqual((website / "public/skill-files/getsentry.json").read_text(encoding="utf-8"), text)
            self.assertEqual((website / S.CATALOG_REL).read_text(encoding="utf-8"), catalog_text)
            self.assertEqual(second["skills"][0]["files"][0]["sha"], "abc")
            # A capture that stored no inline text removes the side file.
            gone = {"skills": [{"id": "getsentry", "slug": "getsentry", "files": [
                {"path": "logo.png", "size": 4, "sha": "def", "omitted": True},
            ]}]}
            S.write_website_catalog(website, gone)
            self.assertFalse((website / "public/skill-files/getsentry.json").exists())

    def test_unsafe_slug_keeps_content_in_the_catalog(self):
        skill = _skill()
        skill["slug"] = "../etc/passwd"
        skill["id"] = "../etc/passwd"
        with tempfile.TemporaryDirectory() as tmp:
            website = Path(tmp)
            written = S.write_website_catalog(website, {"skills": [skill]})
            self.assertEqual(written["skills"][0]["files"][0]["content"], "hello\n")
            self.assertFalse((website / "public/skill-files").exists())

    def test_publish_diff_allows_the_skill_files_directory_only(self):
        self.assertTrue(S.catalog_publish_diff_ok(["public/skills-catalog.json"]))
        self.assertTrue(S.catalog_publish_diff_ok([
            "public/skill-files/getsentry.json",
            "public/skills-catalog.json",
        ]))
        self.assertTrue(S.catalog_publish_diff_ok(["public/skill-files/getsentry.json"]))
        self.assertFalse(S.catalog_publish_diff_ok([]))
        self.assertFalse(S.catalog_publish_diff_ok([
            "public/skills-catalog.json",
            "public/skill-files/nested/getsentry.json",
        ]))
        self.assertFalse(S.catalog_publish_diff_ok(["public/skill-files/../skills-catalog.json"]))
        self.assertFalse(S.catalog_publish_diff_ok(["app/page.tsx"]))


if __name__ == "__main__":
    unittest.main()
