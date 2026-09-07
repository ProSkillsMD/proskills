#!/usr/bin/env python3
"""Deterministic backlog dedupe dry-run for ProSkills submissions.

No GitHub mutations. No AI per issue. No fetching/executing submitted code.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from normalize import is_malformed_url, normalize_repo_url

URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.I)


def extract_primary_source(title: str, body: str) -> str | None:
    text = f"{body or ''}\n{title or ''}"
    urls = [normalize_repo_url(u) for u in URL_RE.findall(text)]
    urls = [u for u in urls if u]
    for u in urls:
        if "github.com" in u:
            return u
    return urls[0] if urls else None


def build(issues_path: Path, catalog_path: Path, out_dir: Path) -> dict:
    payload = json.loads(issues_path.read_text())
    issues = payload["issues"] if isinstance(payload, dict) else payload
    cat = json.loads(catalog_path.read_text())
    published = set()
    for s in cat.get("skills") or []:
        for k in ("repo_url", "repo", "github", "repository"):
            n = normalize_repo_url(s.get(k) if not isinstance(s.get(k), dict) else None)
            if n:
                published.add(n)

    rows = []
    for issue in issues:
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        raw_urls = URL_RE.findall(body + "\n" + title)
        norms = [normalize_repo_url(u) for u in raw_urls]
        has_norm = any(norms)
        malformed = bool(raw_urls) and not has_norm and any(is_malformed_url(u) for u in raw_urls)
        src = extract_primary_source(title, body)
        rows.append({
            "number": issue["number"],
            "createdAt": issue.get("createdAt"),
            "source": src,
            "malformed": malformed,
            "missing": src is None and not malformed,
        })

    by_src: dict[str, list] = {}
    for r in rows:
        if r["source"]:
            by_src.setdefault(r["source"], []).append(r)

    dispositions = []
    for r in rows:
        if r["malformed"]:
            dispositions.append({"number": r["number"], "disposition": "invalid_source", "reason": "malformed URL", "canonical_number": None, "source": None})
            continue
        if r["missing"]:
            dispositions.append({"number": r["number"], "disposition": "missing_source", "reason": "no extractable source URL", "canonical_number": None, "source": None})
            continue
        src = r["source"]
        group = sorted(by_src[src], key=lambda x: (x["createdAt"] or "", x["number"]))
        canonical = group[0]
        if src in published:
            dispositions.append({"number": r["number"], "disposition": "already_published", "reason": "normalized source already in catalog", "canonical_number": canonical["number"], "source": src})
            continue
        if r["number"] == canonical["number"]:
            dispositions.append({"number": r["number"], "disposition": "canonical_actionable", "reason": "earliest valid issue for normalized source", "canonical_number": r["number"], "source": src})
        else:
            dispositions.append({"number": r["number"], "disposition": "normalized_source_duplicate", "reason": f"duplicate of #{canonical['number']}", "canonical_number": canonical["number"], "source": src})

    counts = Counter(d["disposition"] for d in dispositions)
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "backlog-cleanup-manifest.json").write_text(json.dumps({
        "generated_at": generated_at,
        "issue_count": len(issues),
        "disposition_counts": dict(counts),
        "items": dispositions,
        "criteria": {
            "canonical": "earliest createdAt, then lowest number",
            "dedupe_key": "normalized_repo_url from issue body/title",
        },
    }, indent=2) + "\n")
    by_disp: dict[str, list[int]] = {}
    for d in dispositions:
        by_disp.setdefault(d["disposition"], []).append(d["number"])
    for k in by_disp:
        by_disp[k] = sorted(by_disp[k])
    (out_dir / "backlog-execution-manifest.json").write_text(json.dumps({
        "generated_at": generated_at,
        "note": "dry-run plan — issue numbers only; no GitHub mutations",
        "issue_numbers_by_disposition": by_disp,
    }, indent=2) + "\n")
    (out_dir / "backlog-rollback-manifest.json").write_text(json.dumps({
        "generated_at": generated_at,
        "note": "dry-run only — no GitHub mutations performed",
        "issue_numbers_touched": [],
    }, indent=2) + "\n")
    with (out_dir / "backlog-cleanup-summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["disposition", "count"])
        for k, v in sorted(counts.items()):
            w.writerow([k, v])
    summary = {"generated_at": generated_at, "counts": dict(counts), "total": len(dispositions)}
    (out_dir / "backlog-cleanup-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues", type=Path, required=True)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    print(json.dumps(build(args.issues, args.catalog, args.out_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
