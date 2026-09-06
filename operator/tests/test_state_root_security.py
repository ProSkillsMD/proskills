"""Phase 3B: state-root isolation, symlink-safe purge, insecure-for-tests gating."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED_STATE = ROOT / "state"
sys.path.insert(0, str(ROOT / "scripts"))

from _common import (  # noqa: E402
    StateRootError,
    get_state_paths,
    resolve_state_root,
)
from oracle_delivery import run_delivery  # noqa: E402
from pipeline import run_pipeline  # noqa: E402
from purge_artifacts import run_purge  # noqa: E402


def _snapshot_shared_state() -> dict[str, float]:
    """Map of relative paths -> mtime for files under shared state (non-recursive junk ok)."""
    snap: dict[str, float] = {}
    if not SHARED_STATE.exists():
        return snap
    for p in SHARED_STATE.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                snap[str(p.relative_to(SHARED_STATE))] = p.stat().st_mtime
            except OSError:
                pass
    return snap


class TestStateRootSecurity(unittest.TestCase):
    def test_temp_state_root_does_not_touch_shared_state(self) -> None:
        marker = SHARED_STATE / f".phase3b_marker_{os.getpid()}"
        SHARED_STATE.mkdir(parents=True, exist_ok=True)
        marker.write_text("marker", encoding="utf-8")
        before = _snapshot_shared_state()
        before_names = set(before.keys())

        with tempfile.TemporaryDirectory(prefix="proskills_iso_") as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "pipeline.sqlite3"
            state_root = tmp_path / "state"
            out = run_pipeline(
                "iso-001",
                name="safe-skill",
                source="fixture",
                local_path="fixtures/safe-skill",
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            self.assertEqual(out["status"], "ok", out)
            # Artifact must land under temp state-root
            arts = list((state_root / "artifacts").glob("*.json"))
            self.assertTrue(arts, "expected artifact under temp state-root")
            # Shared state must not gain new files
            after = _snapshot_shared_state()
            after_names = set(after.keys())
            new_files = after_names - before_names
            self.assertEqual(
                new_files,
                set(),
                f"tests must not write into shared state; new={new_files}",
            )
            # Marker untouched
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "marker")

        marker.unlink(missing_ok=True)

    def test_reject_symlinked_state_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_symlink_root_") as tmp:
            tmp_path = Path(tmp)
            real = tmp_path / "real_state"
            real.mkdir()
            link = tmp_path / "link_state"
            os.symlink(real, link)
            with self.assertRaises(StateRootError):
                resolve_state_root(link)
            with self.assertRaises(StateRootError):
                get_state_paths(link)

    def test_symlink_cleanup_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_purge_sym_") as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            art = state_root / "artifacts"
            art.mkdir(parents=True)
            outside = tmp_path / "outside_precious.txt"
            outside.write_text("do-not-delete", encoding="utf-8")
            # Symlink inside artifacts pointing outside
            link = art / "escape.json"
            os.symlink(outside, link)
            # Also an old regular file to confirm purge still works
            old = art / "old.json"
            old.write_text('{"x":1}', encoding="utf-8")
            old_mtime = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
            os.utime(old, (old_mtime, old_mtime))
            # Age the symlink mtime too (on the link inode)
            os.utime(link, (old_mtime, old_mtime), follow_symlinks=False)

            db = tmp_path / "db.sqlite3"
            result = run_purge(
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            self.assertEqual(result["status"], "ok", result)
            # Outside target must survive
            self.assertTrue(outside.exists(), "purge must not delete outside symlink target")
            self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-delete")
            # Old regular file removed
            self.assertFalse(old.exists())
            # Symlink itself may be removed (link inode) — target still intact above
            # Either way, outside content preserved.

    def test_insecure_rejected_without_test_env(self) -> None:
        prev = os.environ.pop("PROSKILLS_ENV", None)
        try:
            os.environ["ORACLE_ENDPOINT_URL"] = "https://127.0.0.1:8443/oracle"
            os.environ["ORACLE_BEARER_TOKEN"] = "tok"
            os.environ["ORACLE_ACK_SECRET"] = "ack"
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "db.sqlite3"
                result = run_delivery(
                    db=db,
                    dry_run=True,
                    emit_output=False,
                    insecure_for_tests=True,
                )
                self.assertEqual(result["status"], "error")
                self.assertIn("PROSKILLS_ENV=test", result["message"])
        finally:
            for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                os.environ.pop(k, None)
            if prev is not None:
                os.environ["PROSKILLS_ENV"] = prev

    def test_insecure_rejects_non_loopback_even_in_test(self) -> None:
        prev = os.environ.get("PROSKILLS_ENV")
        os.environ["PROSKILLS_ENV"] = "test"
        try:
            for url in (
                "https://example.com/oracle",
                "https://8.8.8.8/oracle",
            ):
                os.environ["ORACLE_ENDPOINT_URL"] = url
                os.environ["ORACLE_BEARER_TOKEN"] = "tok"
                os.environ["ORACLE_ACK_SECRET"] = "ack"
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "db.sqlite3"
                    result = run_delivery(
                        db=db,
                        dry_run=True,
                        emit_output=False,
                        insecure_for_tests=True,
                    )
                    self.assertEqual(result["status"], "error", result)
                    msg = result["message"].lower()
                    self.assertTrue(
                        "loopback" in msg
                        or "allowed_hostnames" in msg
                        or "non-loopback" in msg,
                        result,
                    )
        finally:
            for k in ("ORACLE_ENDPOINT_URL", "ORACLE_BEARER_TOKEN", "ORACLE_ACK_SECRET"):
                os.environ.pop(k, None)
            if prev is None:
                os.environ.pop("PROSKILLS_ENV", None)
            else:
                os.environ["PROSKILLS_ENV"] = prev

    def test_db_parent_not_used_for_artifacts(self) -> None:
        """When --db and --state-root differ, artifacts land only under state-root."""
        with tempfile.TemporaryDirectory(prefix="proskills_split_") as tmp:
            tmp_path = Path(tmp)
            db_dir = tmp_path / "dbdir"
            db_dir.mkdir()
            db = db_dir / "pipeline.sqlite3"
            state_root = tmp_path / "state_only"
            before_dbdir = set(db_dir.rglob("*"))
            out = run_pipeline(
                "split-001",
                name="safe-skill",
                source="fixture",
                local_path="fixtures/safe-skill",
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            self.assertEqual(out["status"], "ok", out)
            arts = list((state_root / "artifacts").glob("*.json"))
            self.assertTrue(arts)
            # No artifacts under db parent
            after_extra = [
                p
                for p in db_dir.rglob("*")
                if p not in before_dbdir and p != db and "artifact" in p.name.lower()
            ]
            self.assertEqual(after_extra, [])
            locks = state_root / "locks"
            self.assertTrue(locks.is_dir())


if __name__ == "__main__":
    unittest.main()
