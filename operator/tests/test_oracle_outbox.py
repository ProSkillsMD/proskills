"""oracle_outbox validation, idempotency, dry-run default."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _common import connect_db, ensure_schema, iso_now  # noqa: E402
from oracle_outbox import (  # noqa: E402
    PayloadValidationError,
    make_outbound_key,
    run_outbox,
    validate_oracle_payload,
)

INIT = ROOT / "scripts" / "init_state.py"
OUTBOX = ROOT / "scripts" / "oracle_outbox.py"


def _valid_payload(**overrides):
    base = {
        "project": "proskills-md",
        "event_id": "evt-001",
        "stage": "pipeline",
        "status": "completed",
        "item": "safe-skill",
        "summary": "Pipeline completed successfully",
        "evidence_urls": ["https://example.com/evidence/1"],
        "action_required": False,
        "timestamp": "2026-09-06T12:00:00Z",
    }
    base.update(overrides)
    return base


class TestOracleOutbox(unittest.TestCase):
    def test_validate_accepts_good_payload(self) -> None:
        clean = validate_oracle_payload(_valid_payload(), expected_project="proskills-md")
        self.assertEqual(clean["project"], "proskills-md")

    def test_reject_extras(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(extra_field="nope"), expected_project="proskills-md"
            )

    def test_reject_bad_stage(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(stage="merge"), expected_project="proskills-md"
            )

    def test_reject_http_url(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(evidence_urls=["http://example.com/x"]),
                expected_project="proskills-md",
            )

    def test_reject_localhost_url(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(evidence_urls=["https://localhost/x"]),
                expected_project="proskills-md",
            )

    def test_reject_private_ip_url(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(evidence_urls=["https://192.168.1.5/x"]),
                expected_project="proskills-md",
            )

    def test_reject_credentials_in_url(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(evidence_urls=["https://user:pass@example.com/x"]),
                expected_project="proskills-md",
            )

    def test_reject_secret_in_summary(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(summary="token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
                expected_project="proskills-md",
            )

    def test_reject_local_path_in_summary(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(summary="see /tmp/secret.txt for details"),
                expected_project="proskills-md",
            )

    def test_reject_oversized_summary(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_oracle_payload(
                _valid_payload(summary="x" * 2001),
                expected_project="proskills-md",
            )

    def test_dry_run_default_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--apply"],
                check=True,
                capture_output=True,
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(OUTBOX),
                    "--db",
                    str(db),
                    "--event-id",
                    "cli-001",
                    "--stage",
                    "report",
                    "--status",
                    "ok",
                    "--item",
                    "safe-skill",
                    "--summary",
                    "all good",
                    "--evidence-url",
                    "https://example.com/a",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            conn = sqlite3.connect(str(db))
            try:
                n = conn.execute("SELECT COUNT(*) FROM outbound_events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)

    def test_apply_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite3"
            subprocess.run(
                [sys.executable, str(INIT), "--db", str(db), "--apply"],
                check=True,
                capture_output=True,
            )
            payload = _valid_payload(event_id="idem-out-1")
            r1 = run_outbox(payload, db=db, dry_run=False, emit_output=False)
            self.assertEqual(r1["status"], "ok")
            key = r1["outbound_key"]
            r2 = run_outbox(payload, db=db, dry_run=False, emit_output=False)
            self.assertEqual(r2["status"], "duplicate")
            self.assertEqual(r2["outbound_key"], key)
            conn = sqlite3.connect(str(db))
            try:
                n = conn.execute("SELECT COUNT(*) FROM outbound_events").fetchone()[0]
                row = conn.execute(
                    "SELECT attempt_count, next_attempt_at, status FROM outbound_events WHERE outbound_key=?",
                    (key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(n, 1)
            self.assertEqual(row[0], 0)
            self.assertEqual(row[2], "pending")

    def test_outbound_key_deterministic(self) -> None:
        k1 = make_outbound_key("proskills-md", "e", "pipeline", "ok", "item")
        k2 = make_outbound_key("proskills-md", "e", "pipeline", "ok", "item")
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)


if __name__ == "__main__":
    unittest.main()
