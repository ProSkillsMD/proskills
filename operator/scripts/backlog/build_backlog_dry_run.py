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

from normalize import is_malformed_url, normalize_source_identity

URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.I)


def extract_raw_url_strings(title: str, body: str) -> list[str]:
    """Extract candidate http(s) URL strings from title+body (order preserved).

    Body scanning collects http(s) URLs only, to avoid false positives from
    path fragments inside tree/blob links. normalize_source_identity still
    accepts owner/repo shorthand when given that form directly.
    """
    blob = f"{body or ''}\n{title or ''}"
    found: list[str] = []
    seen: set[str] = set()
    for u in URL_RE.findall(blob):
        u = u.rstrip(".,;:)")
        u = u.rstrip("\u3002\uff0c\uff09\u3001")
        if u not in seen:
            seen.add(u)
            found.append(u)
    return found


def extract_all_candidates(title: str, body: str) -> list[dict]:
    """Return unique normalized source identities (deduped by identity_key, order stable)."""
    out: list[dict] = []
    seen_keys: set[str] = set()
    for raw in extract_raw_url_strings(title, body):
        ident = normalize_source_identity(raw)
        if not ident:
            continue
        key = ident["identity_key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(ident)
    return out


def build_catalog_index(cat: dict) -> dict[str, list[dict]]:
    """Map repository URL -> list of {id, repo, subpath} catalog records.

    Live catalog stores root repo_url only; subpath is None unless an explicit
    tree/blob path appears in repo_url (rare).
    """
    by_repo: dict[str, list[dict]] = {}
    for s in cat.get("skills") or []:
        raw = None
        for k in ("repo_url", "repo", "github", "repository"):
            v = s.get(k)
            if isinstance(v, dict):
                v = v.get("url") or v.get("html_url")
            if v:
                raw = v
                break
        ident = normalize_source_identity(str(raw) if raw else None)
        if not ident:
            continue
        rec = {
            "id": s.get("id") or s.get("slug"),
            "repo": ident["repo"],
            "subpath": ident["subpath"],
        }
        by_repo.setdefault(ident["repo"], []).append(rec)
    return by_repo


def published_match(ident: dict, catalog_by_repo: dict[str, list[dict]]) -> tuple[str, list[str]]:
    """Decide already_published support for a single identity.

    Returns (status, skill_ids) where status is:
      - "match": repository + subpath (when available) support already_published
      - "none": not already_published (missing repo, or distinct explicit subpath
        that must not be absorbed by a root catalog record)
      - "conflict": alignment is unclear → ambiguous_manual_review
    """
    records = catalog_by_repo.get(ident["repo"]) or []
    if not records:
        return "none", []

    issue_sub = ident["subpath"]  # None => root submission
    if issue_sub is None:
        # Root-repository submission may match root-repository catalog records.
        root_ids = [r["id"] for r in records if r["subpath"] is None and r["id"]]
        if root_ids:
            return "match", root_ids
        # Catalog has only explicit-subpath records; root must not auto-absorb them
        return "conflict", [r["id"] for r in records if r["id"]]

    # Explicit subpath: match only the same subpath. Distinct subpaths / root catalog
    # records must NOT absorb this submission into already_published.
    exact = [r["id"] for r in records if r["subpath"] == issue_sub and r["id"]]
    if exact:
        return "match", exact
    return "none", []


def classify_issue_row(
    number: int,
    created_at: str | None,
    candidates: list[dict],
    raw_urls: list[str],
) -> dict:
    """Pre-disposition row before group-level canonical assignment."""
    if not candidates:
        malformed = bool(raw_urls) and any(is_malformed_url(u) for u in raw_urls)
        if malformed:
            return {
                "number": number,
                "createdAt": created_at,
                "status": "invalid_source",
                "candidates": [],
                "identity": None,
            }
        return {
            "number": number,
            "createdAt": created_at,
            "status": "missing_source",
            "candidates": [],
            "identity": None,
        }

    keys = {c["identity_key"] for c in candidates}
    if len(keys) > 1:
        return {
            "number": number,
            "createdAt": created_at,
            "status": "ambiguous",
            "candidates": candidates,
            "identity": None,
            "reason": "competing normalized source identities",
        }

    return {
        "number": number,
        "createdAt": created_at,
        "status": "ok",
        "candidates": candidates,
        "identity": candidates[0],
    }


def build(issues_path: Path, catalog_path: Path, out_dir: Path) -> dict:
    payload = json.loads(issues_path.read_text())
    issues = payload["issues"] if isinstance(payload, dict) else payload
    cat = json.loads(catalog_path.read_text())
    catalog_by_repo = build_catalog_index(cat)

    rows = []
    for issue in issues:
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        raw_urls = extract_raw_url_strings(title, body)
        candidates = extract_all_candidates(title, body)
        rows.append(
            classify_issue_row(
                issue["number"],
                issue.get("createdAt"),
                candidates,
                raw_urls,
            )
        )

    # Group actionable identities for canonical selection
    by_key: dict[str, list] = {}
    for r in rows:
        if r["status"] == "ok" and r["identity"]:
            by_key.setdefault(r["identity"]["identity_key"], []).append(r)

    dispositions = []
    for r in rows:
        cand_keys = [c["identity_key"] for c in r["candidates"]]
        if r["status"] == "invalid_source":
            dispositions.append({
                "number": r["number"],
                "disposition": "invalid_source",
                "reason": "malformed URL",
                "canonical_number": None,
                "source": None,
                "subpath": None,
                "candidates": [],
                "published_skill_ids": [],
            })
            continue
        if r["status"] == "missing_source":
            dispositions.append({
                "number": r["number"],
                "disposition": "missing_source",
                "reason": "no extractable source URL",
                "canonical_number": None,
                "source": None,
                "subpath": None,
                "candidates": [],
                "published_skill_ids": [],
            })
            continue
        if r["status"] == "ambiguous":
            dispositions.append({
                "number": r["number"],
                "disposition": "ambiguous_manual_review",
                "reason": r.get("reason") or "competing normalized source identities",
                "canonical_number": None,
                "source": None,
                "subpath": None,
                "candidates": cand_keys,
                "published_skill_ids": [],
            })
            continue

        ident = r["identity"]
        pub_status, skill_ids = published_match(ident, catalog_by_repo)
        if pub_status == "conflict":
            # Repository present but explicit subpath / root alignment unsupported
            dispositions.append({
                "number": r["number"],
                "disposition": "ambiguous_manual_review",
                "reason": (
                    "published repository identity present but explicit subpath "
                    "does not support already_published"
                    if ident["subpath"]
                    else "root submission conflicts with non-root catalog records for repository"
                ),
                "canonical_number": None,
                "source": ident["repo"],
                "subpath": ident["subpath"],
                "candidates": cand_keys,
                "published_skill_ids": skill_ids,
            })
            continue

        if pub_status == "match":
            # Multiple issues may be already_published for the same live skill
            group = sorted(
                by_key[ident["identity_key"]],
                key=lambda x: (x["createdAt"] or "", x["number"]),
            )
            canonical = group[0]
            dispositions.append({
                "number": r["number"],
                "disposition": "already_published",
                "reason": "repository identity and subpath support match to live catalog",
                "canonical_number": canonical["number"],
                "source": ident["repo"],
                "subpath": ident["subpath"],
                "candidates": cand_keys,
                "published_skill_ids": skill_ids,
            })
            continue

        # Not published — canonical / duplicate by identity_key (repo + subpath)
        group = sorted(
            by_key[ident["identity_key"]],
            key=lambda x: (x["createdAt"] or "", x["number"]),
        )
        canonical = group[0]
        if r["number"] == canonical["number"]:
            dispositions.append({
                "number": r["number"],
                "disposition": "canonical_actionable",
                "reason": "earliest valid issue for normalized source identity",
                "canonical_number": r["number"],
                "source": ident["repo"],
                "subpath": ident["subpath"],
                "candidates": cand_keys,
                "published_skill_ids": [],
            })
        else:
            dispositions.append({
                "number": r["number"],
                "disposition": "normalized_source_duplicate",
                "reason": f"duplicate of #{canonical['number']}",
                "canonical_number": canonical["number"],
                "source": ident["repo"],
                "subpath": ident["subpath"],
                "candidates": cand_keys,
                "published_skill_ids": [],
            })

    counts = Counter(d["disposition"] for d in dispositions)
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "backlog-cleanup-manifest.json").write_text(json.dumps({
        "generated_at": generated_at,
        "issue_count": len(issues),
        "catalog_skill_count": len(cat.get("skills") or []),
        "disposition_counts": dict(counts),
        "items": dispositions,
        "criteria": {
            "canonical": "earliest createdAt, then lowest number",
            "dedupe_key": "normalized repo URL + explicit tree/blob subpath when present",
            "multi_url": (
                "all candidate URLs extracted; equivalent identities classify normally; "
                "competing identities => ambiguous_manual_review"
            ),
            "monorepo": (
                "owner/repo is repository identity; explicit tree/blob subpath is secondary "
                "skill identity; distinct subpaths are not duplicates; root submissions may "
                "match root catalog records but do not absorb distinct explicit subpaths"
            ),
            "already_published": (
                "requires repository identity match and subpath alignment when available; "
                "conflicts => ambiguous_manual_review; multiple issues may map to one skill"
            ),
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
    summary = {
        "generated_at": generated_at,
        "counts": dict(counts),
        "total": len(dispositions),
        "catalog_skill_count": len(cat.get("skills") or []),
    }
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
