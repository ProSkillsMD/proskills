"""dry-run leaves DB unchanged / does not create DB when appropriate."""

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


class TestDryRun(unittest.TestCase):
    def test_init_state_dry_run_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            proc = subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--dry-run"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertFalse(db.exists())
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            self.assertIn("would_create_tables", payload)

    def test_intake_dry_run_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            # create empty schema first
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                check=True,
                capture_output=True,
            )
            before = db.read_bytes()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(INTAKE),
                    "--db",
                    str(db),
                    "--event-id",
                    "dry-001",
                    "--name",
                    "safe-skill",
                    "--local-path",
                    "fixtures/safe-skill",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            after = db.read_bytes()
            self.assertEqual(before, after)
            conn = sqlite3.connect(str(db))
            try:
                n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)

    def test_init_state_default_is_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            proc = subprocess.run(
                [sys.executable, str(INIT), "--db", str(db)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertFalse(db.exists())
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])

    def test_intake_default_is_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                check=True,
                capture_output=True,
            )
            before = db.read_bytes()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(INTAKE),
                    "--db",
                    str(db),
                    "--event-id",
                    "dry-default-001",
                    "--name",
                    "safe-skill",
                    "--local-path",
                    "fixtures/safe-skill",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(before, db.read_bytes())
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])


if __name__ == "__main__":
    unittest.main()
