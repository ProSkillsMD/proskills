"""resolve_under_root rejects traversal and absolute escapes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _common import resolve_under_root  # noqa: E402


class TestPathTraversal(unittest.TestCase):
    def test_rejects_dotdot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok").mkdir()
            with self.assertRaises(ValueError):
                resolve_under_root(root, "../etc/passwd")
            with self.assertRaises(ValueError):
                resolve_under_root(root, "ok/../../etc/passwd")

    def test_rejects_absolute_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                resolve_under_root(root, "/etc/passwd")

    def test_allows_relative_inside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixtures" / "safe").mkdir(parents=True)
            resolved = resolve_under_root(root, "fixtures/safe")
            self.assertTrue(str(resolved).startswith(str(root.resolve())))

    def test_allows_absolute_inside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inner = root / "fixtures" / "safe"
            inner.mkdir(parents=True)
            resolved = resolve_under_root(root, str(inner))
            self.assertEqual(resolved, inner.resolve())


if __name__ == "__main__":
    unittest.main()
