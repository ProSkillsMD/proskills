import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catalog_update import flatten_category


class FlattenCategoryTest(unittest.TestCase):
    def test_slash_category_keeps_first_segment(self):
        self.assertEqual(flatten_category("security/osint"), "security")
        self.assertEqual(flatten_category("a\\b"), "a")

    def test_empty_falls_back_to_other(self):
        for v in (None, "", " / "):
            self.assertEqual(flatten_category(v), "other")

    def test_flat_category_unchanged(self):
        self.assertEqual(flatten_category("devops"), "devops")


if __name__ == "__main__":
    unittest.main()
