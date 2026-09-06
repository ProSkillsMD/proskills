#!/usr/bin/env python3
"""Discover and dedupe candidates.

Replaces old Curio role where applicable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import add_dry_run_apply_flags, emit, exit_ok, resolve_dry_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and dedupe candidates (Scout).")
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    dry_run = resolve_dry_run(args)
    emit(
        stage="discover",
        status="ok",
        dry_run=dry_run,
        message="discover stub — dry-run by default; no remote writes without --apply",
    )
    exit_ok()


if __name__ == "__main__":
    main()
