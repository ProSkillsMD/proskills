#!/usr/bin/env python3
"""Create the label set of the issue-based flow (idempotent). Default dry-run; --apply creates missing labels.

Only creates. Never deletes, renames or recolours existing labels (old-flow labels stay as they are).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402


def ensure_labels(api: F.IssueRepo, *, apply: bool) -> dict[str, list[str]]:
    have = {n.lower() for n in api.label_set()}
    created, present, failed = [], [], []
    for name, (color, desc) in F.LABELS.items():
        if name.lower() in have:
            present.append(name)
            continue
        if apply:
            try:
                api.create_label(name, color, desc)
            except scout.Unprocessable:  # created concurrently
                present.append(name)
                continue
            except scout.GitHubError as e:
                failed.append(f"{name}: {type(e).__name__}")
                continue
        created.append(name)
    return {"created" if apply else "would_create": created, "present": present, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    api = F.IssueRepo(scout.GitHubClient(scout.get_gh_token()), write_interval=1.0)
    print(json.dumps(ensure_labels(api, apply=args.apply), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
