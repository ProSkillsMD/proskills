"""Duplicate event_id must not duplicate candidates or stage_runs."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "scripts" / "init_state.py"
INTAKE = ROOT / "scripts" / "intake.py"
FIXTURE = ROOT / "fixtures" / "safe-skill"


class TestIdempotency(unittest.TestCase):
    def test_duplicate_event_id(self) -> None:
        self.assertTrue(FIXTURE.is_dir(), "fixture missing")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                check=True,
                capture_output=True,
            )
            cmd = [
                sys.executable,
                str(INTAKE),
                "--db",
                str(db),
                "--event-id",
                "idem-001",
                "--name",
                "safe-skill",
                "--source",
                "fixture",
                "--source-uri",
                "fixtures/safe-skill",
                "--local-path",
                "fixtures/safe-skill",
                "--apply",
            ]
            r1 = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertEqual(r1.returncode, 0, r1.stderr + r1.stdout)
            out1 = json.loads(r1.stdout.strip().splitlines()[-1])
            self.assertEqual(out1["status"], "ok")

            r2 = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertEqual(r2.returncode, 0, r2.stderr + r2.stdout)
            out2 = json.loads(r2.stdout.strip().splitlines()[-1])
            self.assertEqual(out2["status"], "duplicate")

            conn = sqlite3.connect(str(db))
            try:
                n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                n_cands = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                n_stages = conn.execute(
                    "SELECT COUNT(*) FROM stage_runs WHERE stage='intake'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n_events, 1)
            self.assertEqual(n_cands, 1)
            self.assertEqual(n_stages, 1)


if __name__ == "__main__":
    unittest.main()
