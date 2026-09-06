#!/usr/bin/env python3
"""Stage CLI: catalog_update."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import add_dry_run_apply_flags, emit, exit_ok, resolve_dry_run


def main() -> None:
    parser = argparse.ArgumentParser(description="catalog_update")
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    dry_run = resolve_dry_run(args)
    extra = {"never_merges": True} if "catalog_update" == "create_pr" else {}
    emit(
        stage="catalog_update",
        status="ok",
        dry_run=dry_run,
        message="Catalog update stub — dry-run by default; no write without --apply",
        **extra,
    )
    exit_ok()


if __name__ == "__main__":
    main()
