"""Oversized bytes and too many files rejected by validate."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate import validate_candidate  # noqa: E402


class TestLimits(unittest.TestCase):
    def test_too_many_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "state") as tmp:
            d = Path(tmp)
            (d / "SKILL.md").write_text("# m\n", encoding="utf-8")
            for i in range(12):
                (d / f"f{i}.txt").write_text("x", encoding="utf-8")
            limits = {
                "max_files_per_candidate": 5,
                "max_total_bytes_per_candidate": 5242880,
            }
            result = validate_candidate(d, limits)
            self.assertFalse(result["ok"])
            self.assertTrue(any("file count" in e for e in result["errors"]), result["errors"])

    def test_oversized_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "state") as tmp:
            d = Path(tmp)
            (d / "SKILL.md").write_text("# m\n", encoding="utf-8")
            (d / "big.bin").write_bytes(b"A" * 5000)
            limits = {
                "max_files_per_candidate": 200,
                "max_total_bytes_per_candidate": 1000,
            }
            result = validate_candidate(d, limits)
            self.assertFalse(result["ok"])
            self.assertTrue(any("total bytes" in e for e in result["errors"]), result["errors"])

    def test_within_limits_ok(self) -> None:
        safe = ROOT / "fixtures" / "safe-skill"
        limits = {
            "max_files_per_candidate": 200,
            "max_total_bytes_per_candidate": 5242880,
        }
        result = validate_candidate(safe, limits)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
