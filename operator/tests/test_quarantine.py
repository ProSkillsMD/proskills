"""Critical static finding quarantines candidate and blocks later stages."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pipeline import run_pipeline  # noqa: E402


class TestQuarantine(unittest.TestCase):
    def test_critical_quarantines_on_apply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_quar_") as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "pipeline.sqlite3"
            state_root = tmp_path / "state"
            event_id = "quar-001"
            out = run_pipeline(
                event_id,
                name="flagged-skill",
                source="fixture",
                local_path="fixtures/flagged-skill",
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            self.assertIn(out["status"], ("quarantined", "flagged", "ok"))
            # Must be quarantined due to critical findings
            self.assertEqual(out["status"], "quarantined", out)
            scan = out["stages"].get("static_scan", {})
            self.assertEqual(scan.get("status"), "critical")
            self.assertTrue(scan.get("has_critical") or scan.get("status") == "critical")

            conn = sqlite3.connect(str(db))
            try:
                cand = conn.execute(
                    "SELECT status FROM candidates WHERE event_id = ?", (event_id,)
                ).fetchone()
                stage = conn.execute(
                    "SELECT status FROM stage_runs WHERE event_id = ? AND stage = 'static_scan'",
                    (event_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(cand)
            self.assertEqual(cand[0], "quarantined")
            self.assertEqual(stage[0], "critical")
            # report still ran
            self.assertEqual(out["stages"]["report"]["action"], "ran")
            # Artifacts only under temp state-root
            arts = list((state_root / "artifacts").glob("*")) if (state_root / "artifacts").exists() else []
            self.assertTrue(any(event_id in p.name for p in arts), arts)

    def test_validate_failure_blocks_scan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_valfail_") as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            # Candidate must resolve under PROJECT_ROOT — use fixtures area temp
            with tempfile.TemporaryDirectory(dir=ROOT / "fixtures", prefix="_badcand_") as cand_tmp:
                cand = Path(cand_tmp)
                (cand / "readme.txt").write_text("no marker", encoding="utf-8")
                rel = str(cand.relative_to(ROOT))
                db = tmp_path / "pipeline.sqlite3"
                event_id = "valfail-001"
                out = run_pipeline(
                    event_id,
                    name="badcand",
                    source="fixture",
                    local_path=rel,
                    db=db,
                    state_root=state_root,
                    dry_run=False,
                    emit_output=False,
                )
                self.assertEqual(out["status"], "blocked", out)
                self.assertEqual(out["stages"]["validate"]["status"], "rejected")
                self.assertEqual(out["stages"]["static_scan"]["action"], "blocked")
                self.assertEqual(out["stages"]["report"]["action"], "ran")

    def test_resume_skips_completed_stages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_resume_") as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "pipeline.sqlite3"
            state_root = tmp_path / "state"
            event_id = "resume-001"
            kwargs = dict(
                name="safe-skill",
                source="fixture",
                local_path="fixtures/safe-skill",
                db=db,
                state_root=state_root,
                dry_run=False,
                emit_output=False,
            )
            out1 = run_pipeline(event_id, **kwargs)
            self.assertEqual(out1["status"], "ok", out1)
            out2 = run_pipeline(event_id, **kwargs)
            # Second run should skip completed stages (idempotent)
            self.assertEqual(out2["stages"]["intake"]["action"], "skipped")
            self.assertEqual(out2["stages"]["validate"]["action"], "skipped")
            self.assertEqual(out2["stages"]["static_scan"]["action"], "skipped")
            self.assertEqual(out2["stages"]["report"]["action"], "ran")

            conn = sqlite3.connect(str(db))
            try:
                n_cands = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                n_intake = conn.execute(
                    "SELECT COUNT(*) FROM stage_runs WHERE stage='intake'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n_cands, 1)
            self.assertEqual(n_intake, 1)


if __name__ == "__main__":
    unittest.main()
