"""End-to-end: pipeline --apply → outbox → mock HTTPS deliver once → no duplicate."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from mock_oracle_https import MockOracleHTTPSServer  # noqa: E402
from oracle_delivery import run_delivery  # noqa: E402
from oracle_outbox import run_outbox  # noqa: E402
from pipeline import run_pipeline  # noqa: E402


class TestOracleE2E(unittest.TestCase):
    def test_pipeline_outbox_deliver_once(self) -> None:
        tmp = tempfile.mkdtemp(prefix="proskills_e2e_")
        tmp_path = Path(tmp)
        prev_env = os.environ.get("PROSKILLS_ENV")
        os.environ["PROSKILLS_ENV"] = "test"
        try:
            db = tmp_path / "pipeline.sqlite3"
            state_root = tmp_path / "state"
            event_id = "e2e-safe-001"

            pipe = run_pipeline(
                event_id,
                name="safe-skill",
                source="fixture",
                local_path="fixtures/safe-skill",
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            self.assertEqual(pipe["status"], "ok", pipe)

            payload = {
                "project": "proskills-md",
                "event_id": event_id,
                "stage": "pipeline",
                "status": "completed",
                "item": "safe-skill",
                "summary": "E2E pipeline completed",
                "evidence_urls": ["https://example.com/proskills/e2e"],
                "action_required": False,
                "timestamp": "2026-09-06T12:00:00Z",
            }
            enq = run_outbox(payload, db=db, dry_run=False, emit_output=False)
            self.assertEqual(enq["status"], "ok", enq)
            key = enq["outbound_key"]

            with MockOracleHTTPSServer(
                "e2e-ack-secret", bearer_token="e2e-bearer"
            ) as srv:
                os.environ["ORACLE_ENDPOINT_URL"] = srv.url
                os.environ["ORACLE_BEARER_TOKEN"] = "e2e-bearer"
                os.environ["ORACLE_ACK_SECRET"] = "e2e-ack-secret"
                try:
                    d1 = run_delivery(
                        db=db,
                        outbound_key=key,
                        dry_run=False,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(d1["delivered_count"], 1, d1)
                    self.assertEqual(srv.count_for(key), 1)

                    d2 = run_delivery(
                        db=db,
                        outbound_key=key,
                        dry_run=False,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(d2["already_delivered_count"], 1, d2)
                    self.assertEqual(srv.count_for(key), 1)

                    conn = sqlite3.connect(str(db))
                    try:
                        rows = conn.execute(
                            "SELECT status FROM outbound_events WHERE outbound_key=?",
                            (key,),
                        ).fetchall()
                    finally:
                        conn.close()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0][0], "delivered")
                finally:
                    for k in (
                        "ORACLE_ENDPOINT_URL",
                        "ORACLE_BEARER_TOKEN",
                        "ORACLE_ACK_SECRET",
                    ):
                        os.environ.pop(k, None)
        finally:
            if prev_env is None:
                os.environ.pop("PROSKILLS_ENV", None)
            else:
                os.environ["PROSKILLS_ENV"] = prev_env
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
