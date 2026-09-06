"""oracle_delivery: HTTPS mock, ack HMAC, no duplicate POST, retries, dry-run."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from _common import connect_db, ensure_schema, iso_now  # noqa: E402
from mock_oracle_https import MockOracleHTTPSServer  # noqa: E402
from oracle_delivery import (  # noqa: E402
    compute_ack_sig,
    run_delivery,
    verify_ack,
)
from oracle_outbox import run_outbox  # noqa: E402
from init_state import run_init_state  # noqa: E402


def _with_test_env():
    """Context-like helper: set PROSKILLS_ENV=test for insecure TLS mocks."""
    prev = os.environ.get("PROSKILLS_ENV")
    os.environ["PROSKILLS_ENV"] = "test"
    return prev


def _restore_env(prev):
    if prev is None:
        os.environ.pop("PROSKILLS_ENV", None)
    else:
        os.environ["PROSKILLS_ENV"] = prev



def _payload(event_id: str = "del-001"):
    return {
        "project": "proskills-md",
        "event_id": event_id,
        "stage": "pipeline",
        "status": "completed",
        "item": "safe-skill",
        "summary": "done",
        "evidence_urls": ["https://example.com/ev"],
        "action_required": False,
        "timestamp": "2026-09-06T12:00:00Z",
    }


class TestOracleDelivery(unittest.TestCase):
    def test_ack_hmac(self) -> None:
        key = "abc123"
        secret = "ack-secret-value"
        sig = compute_ack_sig(key, secret)
        self.assertTrue(verify_ack({"outbound_key": key, "status": "accepted", "sig": sig}, key, secret))
        self.assertFalse(
            verify_ack({"outbound_key": key, "status": "accepted", "sig": "deadbeef"}, key, secret)
        )

    def test_dry_run_no_post(self) -> None:
        prev = _with_test_env()
        try:
            self._run_test_dry_run_no_post()
        finally:
            _restore_env(prev)

    def _run_test_dry_run_no_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            run_init_state(db=db, state_root=Path(tmp)/"state", dry_run=False, emit_output=False)
            run_outbox(_payload(), db=db, dry_run=False, emit_output=False)
            with MockOracleHTTPSServer("sec", bearer_token="tok") as srv:
                os.environ["ORACLE_ENDPOINT_URL"] = srv.url
                os.environ["ORACLE_BEARER_TOKEN"] = "tok"
                os.environ["ORACLE_ACK_SECRET"] = "sec"
                try:
                    result = run_delivery(
                        db=db, dry_run=True, emit_output=False, insecure_for_tests=True
                    )
                    self.assertTrue(result["dry_run"])
                    self.assertEqual(srv.state.total_posts, 0)
                finally:
                    for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                        os.environ.pop(k, None)

    def test_deliver_success_and_no_duplicate(self) -> None:
        prev = _with_test_env()
        try:
            self._run_test_deliver_success_and_no_duplicate()
        finally:
            _restore_env(prev)

    def _run_test_deliver_success_and_no_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            run_init_state(db=db, state_root=Path(tmp)/"state", dry_run=False, emit_output=False)
            enq = run_outbox(_payload("del-dup-1"), db=db, dry_run=False, emit_output=False)
            key = enq["outbound_key"]
            with MockOracleHTTPSServer("ack-secret", bearer_token="bearer-tok") as srv:
                os.environ["ORACLE_ENDPOINT_URL"] = srv.url
                os.environ["ORACLE_BEARER_TOKEN"] = "bearer-tok"
                os.environ["ORACLE_ACK_SECRET"] = "ack-secret"
                try:
                    r1 = run_delivery(
                        db=db,
                        outbound_key=key,
                        dry_run=False,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(r1["delivered_count"], 1, r1)
                    self.assertEqual(srv.count_for(key), 1)

                    r2 = run_delivery(
                        db=db,
                        outbound_key=key,
                        dry_run=False,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(r2["already_delivered_count"], 1, r2)
                    self.assertEqual(srv.count_for(key), 1)  # no second POST

                    conn = sqlite3.connect(str(db))
                    try:
                        row = conn.execute(
                            "SELECT status, delivered_at FROM outbound_events WHERE outbound_key=?",
                            (key,),
                        ).fetchone()
                    finally:
                        conn.close()
                    self.assertEqual(row[0], "delivered")
                    self.assertTrue(row[1])
                finally:
                    for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                        os.environ.pop(k, None)

    def test_bad_ack_sig_increments_attempt(self) -> None:
        prev = _with_test_env()
        try:
            self._run_test_bad_ack_sig_increments_attempt()
        finally:
            _restore_env(prev)

    def _run_test_bad_ack_sig_increments_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            run_init_state(db=db, state_root=Path(tmp)/"state", dry_run=False, emit_output=False)
            enq = run_outbox(_payload("del-bad-ack"), db=db, dry_run=False, emit_output=False)
            key = enq["outbound_key"]
            with MockOracleHTTPSServer("server-secret", bearer_token="tok") as srv:
                os.environ["ORACLE_ENDPOINT_URL"] = srv.url
                os.environ["ORACLE_BEARER_TOKEN"] = "tok"
                # Wrong ack secret → verify fails
                os.environ["ORACLE_ACK_SECRET"] = "wrong-secret"
                try:
                    r1 = run_delivery(
                        db=db,
                        outbound_key=key,
                        dry_run=False,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(r1["delivered_count"], 0, r1)
                    conn = sqlite3.connect(str(db))
                    try:
                        row = conn.execute(
                            "SELECT status, attempt_count, next_attempt_at FROM outbound_events WHERE outbound_key=?",
                            (key,),
                        ).fetchone()
                    finally:
                        conn.close()
                    self.assertEqual(row[0], "pending")
                    self.assertEqual(row[1], 1)
                    self.assertTrue(row[2])
                finally:
                    for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                        os.environ.pop(k, None)

    def test_token_not_printed_on_error(self) -> None:
        prev = _with_test_env()
        try:
            self._run_test_token_not_printed_on_error()
        finally:
            _restore_env(prev)

    def _run_test_token_not_printed_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            run_init_state(db=db, state_root=Path(tmp)/"state", dry_run=False, emit_output=False)
            secret_token = "super-secret-bearer-token-xyz"
            os.environ["ORACLE_ENDPOINT_URL"] = "https://127.0.0.1:1/nope"
            os.environ["ORACLE_BEARER_TOKEN"] = secret_token
            os.environ["ORACLE_ACK_SECRET"] = "ack"
            try:
                run_outbox(_payload("del-print"), db=db, dry_run=False, emit_output=False)
                result = run_delivery(
                    db=db, dry_run=False, emit_output=False, insecure_for_tests=True
                )
                blob = json.dumps(result)
                self.assertNotIn(secret_token, blob)
            finally:
                for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                    os.environ.pop(k, None)

    def test_hostname_not_allowlisted_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            os.environ["ORACLE_ENDPOINT_URL"] = "https://evil.example.com/oracle"
            os.environ["ORACLE_BEARER_TOKEN"] = "tok"
            os.environ["ORACLE_ACK_SECRET"] = "ack"
            try:
                result = run_delivery(db=db, dry_run=False, emit_output=False)
                self.assertEqual(result["status"], "error")
                self.assertIn("allowed_hostnames", result["message"])
            finally:
                for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
