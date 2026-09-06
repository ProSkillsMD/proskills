#!/usr/bin/env python3
"""Stage CLI: create_pr."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import add_dry_run_apply_flags, emit, exit_ok, resolve_dry_run


def main() -> None:
    parser = argparse.ArgumentParser(description="create_pr")
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    dry_run = resolve_dry_run(args)
    extra = {"never_merges": True} if "create_pr" == "create_pr" else {}
    emit(
        stage="create_pr",
        status="ok",
        dry_run=dry_run,
        message="PR stub — dry-run by default; never merges; GitHub deferred until Oracle bridge",
        **extra,
    )
    exit_ok()


if __name__ == "__main__":
    main()
