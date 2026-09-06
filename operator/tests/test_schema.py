"""init_state creates required tables."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "scripts" / "init_state.py"

REQUIRED_TABLES = {
    "events",
    "candidates",
    "stage_runs",
    "artifacts",
    "approvals",
    "outbound_events",
}


class TestSchema(unittest.TestCase):
    def test_init_state_creates_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            proc = subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue(db.exists())
            conn = sqlite3.connect(str(db))
            try:
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()
            self.assertTrue(REQUIRED_TABLES.issubset(names), names)

    def test_unique_constraints_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                check=True,
                capture_output=True,
            )
            conn = sqlite3.connect(str(db))
            try:
                # candidates.event_id unique
                conn.execute(
                    "INSERT INTO events VALUES ('e1','s','{}','t','ok')"
                )
                conn.execute(
                    "INSERT INTO candidates (event_id,name,status,created_at) VALUES ('e1','n','ok','t')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO candidates (event_id,name,status,created_at) VALUES ('e1','n2','ok','t')"
                    )
                # stage_runs unique (event_id, stage)
                conn.execute(
                    "INSERT INTO stage_runs (event_id,stage,status) VALUES ('e1','intake','ok')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO stage_runs (event_id,stage,status) VALUES ('e1','intake','ok')"
                    )
            finally:
                conn.close()

    def test_outbound_events_has_retry_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--state-root", str(Path(tmp) / "state"), "--apply"],
                check=True,
                capture_output=True,
            )
            conn = sqlite3.connect(str(db))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(outbound_events)")}
            finally:
                conn.close()
            self.assertIn("outbound_key", cols)
            self.assertIn("attempt_count", cols)
            self.assertIn("next_attempt_at", cols)
            self.assertIn("body_json", cols)
            self.assertIn("delivered_at", cols)

    def test_migrate_adds_columns_to_legacy_table(self) -> None:
        """ensure_schema migrates older outbound_events missing retry columns."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            conn = sqlite3.connect(str(db))
            try:
                conn.executescript(
                    """
                    CREATE TABLE events (
                        event_id TEXT PRIMARY KEY, source TEXT, payload_json TEXT,
                        received_at TEXT, status TEXT
                    );
                    CREATE TABLE outbound_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        outbound_key TEXT NOT NULL UNIQUE,
                        stage TEXT, status TEXT, body_json TEXT,
                        created_at TEXT, delivered_at TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()
            sys.path.insert(0, str(ROOT / "scripts"))
            from _common import connect_db, ensure_schema
            conn = connect_db(db)
            try:
                ensure_schema(conn)
                conn.commit()
                cols = {r[1] for r in conn.execute("PRAGMA table_info(outbound_events)")}
            finally:
                conn.close()
            self.assertIn("attempt_count", cols)
            self.assertIn("next_attempt_at", cols)


if __name__ == "__main__":
    unittest.main()
