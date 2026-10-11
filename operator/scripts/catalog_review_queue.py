#!/usr/bin/env python3
"""Build a prioritized backlog of catalog skills with reviewed!=true.

Reads a catalog JSON from a local path or live URL (curl with cache-bust; urllib
often gets 403 on proskills.md). Optionally enriches with cheap GitHub signals
via `gh api` (stars, pushed_at, license SPDX). Ranks stars/activity first.

  python3 catalog_review_queue.py --catalog /path/skills-catalog.json --out queue.json --limit 20
  python3 catalog_review_queue.py --catalog https://proskills.md/skills-catalog.json --limit 20
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, exit_fail, exit_ok  # noqa: E402
from publish_lib import parse_github_source  # noqa: E402


def _live_url() -> str:
    try:
        from publish_lib import LIVE_CATALOG_URL as u
        return u
    except Exception:
        return "https://proskills.md/skills-catalog.json"


def fetch_catalog_bytes(url: str) -> bytes:
    """Fetch catalog via curl (python urllib often gets 403 on proskills.md)."""
    cb = datetime.now(timezone.utc).strftime("%s%f")
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}cb={cb}"
    proc = subprocess.run(
        ["curl", "-sS", "-L", "-A", "proskills-operator/1.0", "-o", "-", full],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:300]}")
    if not proc.stdout:
        raise RuntimeError("curl returned empty body")
    return proc.stdout


def load_catalog_source(source: str | Path) -> dict[str, Any]:
    text = str(source)
    if text.startswith("http://") or text.startswith("https://"):
        raw = fetch_catalog_bytes(text)
        return json.loads(raw.decode("utf-8"))
    path = Path(text)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_owner_repo(repo_url: str | None) -> tuple[str, str] | None:
    if not repo_url:
        return None
    parsed = parse_github_source(repo_url)
    if parsed and parsed.get("owner") and parsed.get("repo"):
        return str(parsed["owner"]), str(parsed["repo"])
    u = urlparse(str(repo_url))
    if "github.com" not in (u.netloc or "").lower():
        return None
    parts = [p for p in (u.path or "").strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


def gh_repo_meta(owner: str, repo: str) -> dict[str, Any] | None:
    """Cheap single-repo metadata via gh api. Returns None on failure."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}",
         "--jq", "{stars: .stargazers_count, pushed_at: .pushed_at, license: .license.spdx_id, archived: .archived}"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def unreviewed_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    skills = catalog.get("skills") or []
    return [s for s in skills if isinstance(s, dict) and s.get("reviewed") is not True]


def enrich_row(skill: dict[str, Any], *, use_gh: bool, cache: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": skill.get("id"),
        "slug": skill.get("slug"),
        "name": skill.get("name"),
        "category": skill.get("category"),
        "repo_url": skill.get("repo_url"),
        "skill_path": skill.get("skill_path"),
        "source_type": skill.get("source_type"),
        "github_stars": int(skill.get("github_stars") or 0),
        "last_commit": skill.get("last_commit") or "",
        "pushed_at": "",
        "license_spdx": skill.get("license") if isinstance(skill.get("license"), str) else None,
        "has_skill_md": bool(skill.get("skill_md")),
        "files_found": list(skill.get("files_found") or []),
        "priority_score": 0.0,
    }
    or_pair = _parse_owner_repo(skill.get("repo_url"))
    if use_gh and or_pair:
        key = f"{or_pair[0]}/{or_pair[1]}".lower()
        if key not in cache:
            cache[key] = gh_repo_meta(or_pair[0], or_pair[1])
        meta = cache.get(key)
        if meta:
            if meta.get("stars") is not None:
                row["github_stars"] = int(meta["stars"])
            if meta.get("pushed_at"):
                row["pushed_at"] = meta["pushed_at"]
            if meta.get("license"):
                row["license_spdx"] = meta["license"]
            row["archived"] = bool(meta.get("archived"))
    # Priority: stars first, then recent activity (pushed_at / last_commit)
    activity = row["pushed_at"] or row["last_commit"] or ""
    row["priority_score"] = float(row["github_stars"]) + (0.001 if activity else 0.0)
    row["activity_at"] = activity
    return row


def rank_by_priority(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stars descending, then activity_at descending (ISO), empty activity last, then slug."""
    def key(r: dict[str, Any]) -> tuple:
        stars = int(r.get("github_stars") or 0)
        act = str(r.get("activity_at") or "")
        # Sort key for activity DESC among non-empty: use complement; empty -> least
        act_rank = act if act else ""
        return (stars, act_rank != "", act_rank, str(r.get("slug") or ""))
    # high stars first => reverse on stars; among equals prefer has_activity then later act
    return sorted(rows, key=key, reverse=True)


def build_queue(
    catalog: dict[str, Any],
    *,
    limit: int | None = None,
    use_gh: bool = False,
) -> dict[str, Any]:
    # Rank on catalog-local signals first; only enrich the truncated head via gh.
    cache: dict[str, Any] = {}
    base_rows = [enrich_row(s, use_gh=False, cache=cache) for s in unreviewed_rows(catalog)]
    rows_sorted = rank_by_priority(base_rows)
    unreviewed_total = len(base_rows)
    if limit is not None and limit >= 0:
        rows_sorted = rows_sorted[:limit]
    if use_gh:
        # Re-enrich selected rows with gh (mutates via fresh enrich from original fields)
        by_id = {str(s.get("id") or s.get("slug")): s for s in unreviewed_rows(catalog)}
        enriched = []
        for r in rows_sorted:
            src = by_id.get(str(r.get("id") or "")) or by_id.get(str(r.get("slug") or "")) or r
            enriched.append(enrich_row(src if "repo_url" in src else r, use_gh=True, cache=cache))
        rows_sorted = rank_by_priority(enriched)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catalog_total": int(catalog.get("total") or len(catalog.get("skills") or [])),
        "unreviewed_total": unreviewed_total,
        "limit": limit,
        "enriched_gh": use_gh,
        "items": rows_sorted,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build prioritized unreviewed catalog queue")
    ap.add_argument("--catalog", default=_live_url(), help="Catalog path or https URL")
    ap.add_argument("--out", type=Path, help="Write queue JSON here (stdout if omitted)")
    ap.add_argument("--limit", type=int, default=None, help="Max queue items")
    ap.add_argument("--enrich-gh", action="store_true", help="Call gh api for stars/pushed_at/license")
    args = ap.parse_args()
    try:
        catalog = load_catalog_source(args.catalog)
    except Exception as exc:
        exit_fail(f"load catalog: {exc}")
        return
    queue = build_queue(catalog, limit=args.limit, use_gh=args.enrich_gh)
    text = json.dumps(queue, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        emit(stage="catalog_review_queue", status="ok", dry_run=True,
             message=f"wrote {len(queue['items'])} items to {args.out}")
    else:
        sys.stdout.write(text)
    exit_ok()


if __name__ == "__main__":
    main()
