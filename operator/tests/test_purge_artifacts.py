"""purge_artifacts: dry-run leaves files; apply removes old; keeps recent; no secrets printed."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _common import connect_db, ensure_schema, iso_now  # noqa: E402
from purge_artifacts import run_purge  # noqa: E402

PURGE = ROOT / "scripts" / "purge_artifacts.py"
INIT = ROOT / "scripts" / "init_state.py"


class TestPurgeArtifacts(unittest.TestCase):
    def _make_env(self, tmp: Path):
        state_root = tmp / "state"
        state_root.mkdir()
        db = tmp / "pipeline.sqlite3"
        art = state_root / "artifacts"
        art.mkdir()
        subprocess.run(
            [
                sys.executable,
                str(INIT),
                "--db",
                str(db),
                "--state-root",
                str(state_root),
                "--apply",
            ],
            check=True,
            capture_output=True,
        )
        return db, state_root, art

    def test_dry_run_leaves_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_purge_") as tmp:
            tmp_path = Path(tmp)
            db, state_root, art = self._make_env(tmp_path)
            old_file = art / "old-evidence.json"
            old_file.write_text('{"ok": true}', encoding="utf-8")
            old_mtime = (datetime.now(timezone.utc) - timedelta(days=45)).timestamp()
            os.utime(old_file, (old_mtime, old_mtime))

            before = old_file.read_bytes()
            result = run_purge(
                db=db,
                state_root=state_root,
                dry_run=True,
                emit_output=False,
            )
            self.assertTrue(result["dry_run"])
            self.assertTrue(old_file.exists())
            self.assertEqual(old_file.read_bytes(), before)
            self.assertGreaterEqual(result["file_count"], 1)

    def test_apply_removes_old_keeps_recent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_purge_") as tmp:
            tmp_path = Path(tmp)
            db, state_root, art = self._make_env(tmp_path)

            old_file = art / "old-evidence.json"
            recent_file = art / "recent-evidence.json"
            old_file.write_text('{"kind":"old"}', encoding="utf-8")
            recent_file.write_text('{"kind":"recent"}', encoding="utf-8")
            old_mtime = (datetime.now(timezone.utc) - timedelta(days=45)).timestamp()
            os.utime(old_file, (old_mtime, old_mtime))

            conn = connect_db(db)
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO events (event_id, source, received_at, status) VALUES (?,?,?,?)",
                ("e-old", "t", iso_now(), "ok"),
            )
            conn.execute(
                "INSERT INTO events (event_id, source, received_at, status) VALUES (?,?,?,?)",
                ("e-new", "t", iso_now(), "ok"),
            )
            old_created = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            new_created = iso_now()
            conn.execute(
                """INSERT INTO artifacts (event_id, stage, kind, path, meta_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                ("e-old", "static_scan", "scan_report", "artifacts/old-evidence.json", '{"r":1}', old_created),
            )
            conn.execute(
                """INSERT INTO artifacts (event_id, stage, kind, path, meta_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                ("e-new", "static_scan", "scan_report", "artifacts/recent-evidence.json", '{"r":2}', new_created),
            )
            conn.commit()
            conn.close()

            result = run_purge(
                db=db, state_root=state_root, dry_run=False, emit_output=False
            )
            self.assertFalse(result["dry_run"])
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())

            conn = sqlite3.connect(str(db))
            try:
                ids = [r[0] for r in conn.execute("SELECT event_id FROM artifacts")]
            finally:
                conn.close()
            self.assertEqual(ids, ["e-new"])

    def test_never_prints_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_purge_") as tmp:
            tmp_path = Path(tmp)
            db, state_root, art = self._make_env(tmp_path)

            secret = "sk_test_abcdefghijklmnopqrstuvwxyz1234"
            f = art / "leaky.json"
            f.write_text(json.dumps({"note": "x"}), encoding="utf-8")
            old_mtime = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
            os.utime(f, (old_mtime, old_mtime))

            conn = connect_db(db)
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO events (event_id, source, received_at, status) VALUES (?,?,?,?)",
                ("e-sec", "t", iso_now(), "ok"),
            )
            old_created = (datetime.now(timezone.utc) - timedelta(days=50)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            conn.execute(
                """INSERT INTO artifacts (event_id, stage, kind, path, meta_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    "e-sec",
                    "static_scan",
                    "scan_report",
                    "artifacts/leaky.json",
                    json.dumps({"token": secret}),
                    old_created,
                ),
            )
            conn.commit()
            conn.close()

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PURGE),
                    "--db",
                    str(db),
                    "--state-root",
                    str(state_root),
                    "--apply",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            combined = proc.stdout + proc.stderr
            self.assertNotIn(secret, combined)
            self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()
