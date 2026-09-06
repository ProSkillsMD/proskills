"""Two processes cannot handle the same event simultaneously."""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _common import EventLockHeld, event_lock  # noqa: E402
from pipeline import run_pipeline  # noqa: E402


def _hold_lock(event_id: str, locks_dir: str, ready: mp.Queue, release: mp.Queue) -> None:
    try:
        with event_lock(event_id, locks_dir, blocking=False):
            ready.put("held")
            release.get(timeout=30)
    except EventLockHeld:
        ready.put("failed_hold")


def _run_pipeline_worker(
    event_id: str,
    db: str,
    state_root: str,
    local_path: str,
    result_q: mp.Queue,
) -> None:
    out = run_pipeline(
        event_id,
        name="safe-skill",
        source="fixture",
        local_path=local_path,
        db=db,
        state_root=state_root,
        dry_run=False,
        emit_output=False,
    )
    result_q.put(out)


class TestConcurrency(unittest.TestCase):
    def test_second_process_gets_locked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_conc_") as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "pipeline.sqlite3"
            state_root = tmp_path / "state"
            locks = state_root / "locks"
            locks.mkdir(parents=True)
            event_id = "conc-001"
            local_path = "fixtures/safe-skill"

            ready: mp.Queue = mp.Queue()
            release: mp.Queue = mp.Queue()
            holder = mp.Process(
                target=_hold_lock, args=(event_id, str(locks), ready, release)
            )
            holder.start()
            self.assertEqual(ready.get(timeout=10), "held")

            result_q: mp.Queue = mp.Queue()
            worker = mp.Process(
                target=_run_pipeline_worker,
                args=(event_id, str(db), str(state_root), local_path, result_q),
            )
            worker.start()
            out = result_q.get(timeout=30)
            worker.join(timeout=10)
            release.put("done")
            holder.join(timeout=10)

            self.assertEqual(out.get("status"), "locked")
            self.assertEqual(worker.exitcode, 0)

    def test_event_lock_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proskills_lock_") as tmp:
            locks = Path(tmp) / "locks"
            locks.mkdir()
            with event_lock("lock-test", locks, blocking=False):
                with self.assertRaises(EventLockHeld):
                    with event_lock("lock-test", locks, blocking=False):
                        pass


if __name__ == "__main__":
    unittest.main()
