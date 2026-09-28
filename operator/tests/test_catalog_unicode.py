import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from publish_lib import catalog_json_dumps, load_build_break_skips, sanitize_text


class CatalogUnicodeTests(unittest.TestCase):
    def test_literal_surrogate_pair_becomes_real_character(self):
        text = sanitize_text(r"\ud83d\udd12")
        self.assertEqual(text, "🔒")
        self.assertEqual(text.encode("utf-8"), b"\xf0\x9f\x94\x92")

    def test_actual_and_literal_lone_surrogates_are_replaced(self):
        text = sanitize_text("actual" + chr(0xD800) + " " + r"\udc00")
        self.assertEqual(text, "actual� �")
        text.encode("utf-8")

    def test_invalid_unicode_escape_is_replaced(self):
        text = sanitize_text(r"bad \u12G4")
        self.assertEqual(text, "bad �12G4")

    def test_catalog_dump_is_utf8_and_round_trips(self):
        dumped = catalog_json_dumps({
            "description": r"lock: \ud83d\udd12",
            "readme": "lone: " + chr(0xDFFF),
            "skill_md": r"literal \u0041",
        })
        encoded = dumped.encode("utf-8")
        loaded = json.loads(encoded.decode("utf-8"))
        self.assertEqual(loaded["description"], "lock: 🔒")
        self.assertEqual(loaded["readme"], "lone: �")
        self.assertEqual(loaded["skill_md"], "literal A")

    def test_build_break_skip_registry_accepts_issue_and_slug_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "skips.json"
            path.write_text(json.dumps({"issues": {"3173": "legacy"}, "slugs": {"cyberlens-skill": "legacy"}}))
            skips = load_build_break_skips(path)
        self.assertEqual(skips["issues"], {3173})
        self.assertEqual(skips["slugs"], {"cyberlens-skill"})


if __name__ == "__main__":
    unittest.main()
