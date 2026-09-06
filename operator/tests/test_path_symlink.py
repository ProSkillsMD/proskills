"""Symlink escape detection and path traversal rejection."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _common import resolve_under_root  # noqa: E402
from static_scan import scan_candidate  # noqa: E402


class TestPathSymlink(unittest.TestCase):
    def test_path_traversal_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_under_root(ROOT, "../etc/passwd")
        with self.assertRaises(ValueError):
            resolve_under_root(ROOT, "fixtures/../../etc/passwd")

    def test_symlink_escape_detected(self) -> None:
        # Create under project state so resolve_under_root / project root checks apply
        base = ROOT / "state" / "_tmp_symlink_test"
        if base.exists():
            # cleanup leftovers
            for p in sorted(base.rglob("*"), reverse=True):
                if p.is_symlink() or p.is_file():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
            try:
                base.rmdir()
            except OSError:
                pass
        base.mkdir(parents=True, exist_ok=True)
        try:
            cand = base / "cand"
            cand.mkdir()
            (cand / "SKILL.md").write_text("# symlink test\n", encoding="utf-8")
            # Symlink pointing outside candidate (and outside if possible to /tmp)
            outside = base / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            link = cand / "escape.link"
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(outside, link)

            # Also a symlink that escapes PROJECT_ROOT if we can
            escape_root = cand / "root_escape.link"
            if escape_root.exists() or escape_root.is_symlink():
                escape_root.unlink()
            os.symlink("/etc/passwd", escape_root)

            limits = {
                "max_files_per_candidate": 200,
                "max_total_bytes_per_candidate": 5242880,
            }
            result = scan_candidate(cand, limits, project_root=ROOT)
            self.assertTrue(result["has_critical"], result)
            rules = {f["rule"] for f in result["findings"]}
            self.assertIn("symlink_escape", rules)
            self.assertGreaterEqual(result["symlink_escapes"], 1)
        finally:
            for p in sorted(base.rglob("*"), reverse=True):
                try:
                    if p.is_symlink() or p.is_file():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        p.rmdir()
                except OSError:
                    pass
            try:
                base.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
