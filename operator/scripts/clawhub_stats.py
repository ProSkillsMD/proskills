#!/usr/bin/env python3
"""Refresh ClawHub download / install / star counts for ClawHub-linked catalog skills.

Reads only public ClawHub skill pages (`https://clawhub.ai/<owner>/skills/<slug>`), through
`sources.clawhub.ClawHubClient`: robots.txt is checked (`/api/` is never called), at least 2 s between
requests, and a hard per-run fetch cap. The catalog itself is the cache: each refreshed record gets
`external_ratings.clawhub_stats_at`, and records are only fetched again once that is older than the TTL
(default 24 h). Stalest records go first, so the cap spreads the work across runs.

Catalog changes are append-only inside `external_ratings`:
  clawhub_downloads, clawhub_installs, clawhub_stars, clawhub_comments   (ints from the page payload)
  clawhub_stats_at      ISO-8601 UTC time of the last check
  clawhub_stats_status  "ok" | "unresolved" (page not found for any owner candidate)
  clawhub_url           rewritten to the canonical `/<owner>/skills/<slug>` form once resolved
                        (legacy `/<owner>/<slug>` URLs redirect there; `/skills/<slug>` URLs are broken)
Never touches id, slug, category, repo_url, source_url or any other top-level field.

Usage (dry-run by default):
  python3 operator/scripts/clawhub_stats.py --catalog ../website/public/skills-catalog.json [--apply]
      [--max-fetches 25] [--ttl-hours 24] [--only slug1,slug2]
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources import clawhub as C  # noqa: E402
from sources.base import STATE, DiskCache, iso, parse_iso  # noqa: E402

STAT_KEYS = ("downloads", "installs", "stars", "comments")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
RESERVED_OWNERS = frozenset({"skills", "plugins", "official", "docs", "api", "admin", "search", "u", "clawhub"})
CANON_RE = re.compile(r'<link rel="canonical" href="https://clawhub\.ai/([A-Za-z0-9_.-]+)/skills/([A-Za-z0-9_.-]+)"')
UNRESOLVED_RETRY_FACTOR = 7  # unresolved records are retried after 7 x TTL


def _clawhub_urls(skill: dict[str, Any]) -> list[str]:
    er = skill.get("external_ratings") or {}
    out = []
    for u in (er.get("clawhub_url"), skill.get("repo_url"), skill.get("source_url"), skill.get("author_url")):
        if isinstance(u, str) and "clawhub.ai/" in u and u not in out:
            out.append(u)
    return out


def parse_clawhub_url(url: str) -> tuple[str | None, str | None]:
    """(owner|None, slug|None) from any ClawHub skill URL form we have seen in the catalog."""
    path = url.split("clawhub.ai", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None, None
    if len(parts) >= 3 and parts[-2] == "skills":          # /owner/skills/slug
        owner, slug = parts[-3].lstrip("@"), parts[-1]
    elif len(parts) == 2 and parts[0] == "skills":          # /skills/slug (owner unknown)
        owner, slug = None, parts[1]
    elif len(parts) == 2:                                  # /owner/slug or /@owner/slug (legacy)
        owner, slug = parts[0].lstrip("@"), parts[1]
    else:
        return None, None
    if owner and (not NAME_RE.match(owner) or owner.lower() in RESERVED_OWNERS):
        owner = None
    if not NAME_RE.match(slug) or slug.lower() in RESERVED_OWNERS:
        return owner, None
    return owner, slug


def _gh_owner(url: Any) -> str | None:
    m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)", str(url or ""))
    return m.group(1) if m else None


def is_clawhub_linked(skill: dict[str, Any]) -> bool:
    return skill.get("source_type") == "clawhub" or bool(_clawhub_urls(skill))


def target(skill: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(clawhub slug, owner candidates in priority order, max 3, case-insensitively unique)."""
    slug, owners = None, []
    for u in _clawhub_urls(skill):
        o, s = parse_clawhub_url(u)
        slug = slug or s
        if o:
            owners.append(o)
    if slug is None:
        return None, []
    er = skill.get("external_ratings") or {}
    owners += [er.get("clawhub_owner"), skill.get("author"), _gh_owner(skill.get("author_github")),
               _gh_owner(skill.get("author_url")), _gh_owner(skill.get("repo_url"))]
    seen, out = set(), []
    for o in owners:
        if isinstance(o, str) and NAME_RE.match(o) and o.lower() not in RESERVED_OWNERS and o.lower() not in seen:
            seen.add(o.lower())
            out.append(o)
    return slug, out[:3]


def _due(er: dict[str, Any], now: datetime, ttl: timedelta) -> tuple[bool, float]:
    at = parse_iso(er.get("clawhub_stats_at"))
    if at is None:
        return True, 0.0
    wait = ttl * (UNRESOLVED_RETRY_FACTOR if er.get("clawhub_stats_status") == "unresolved" else 1)
    return now - at >= wait, at.timestamp()


class PageStats:
    """Fetches one page through the polite client and extracts (canonical owner, slug, stats)."""

    def __init__(self, client: C.ClawHubClient):
        self.client = client

    def __call__(self, owner: str, slug: str) -> tuple[str, str, dict[str, int]] | None:
        status, text = self.client._get(C.page_url(owner, slug))  # robots + /api/ guard + min interval
        if status != 200 or not text:
            return None
        stats = C.parse_page(text).get("stats") or {}
        if "downloads" not in stats:  # ClawHub renders a 200 shell for unknown owner/slug pairs
            return None
        m = CANON_RE.search(text)
        return (m.group(1), m.group(2), stats) if m else (owner, slug, stats)


def refresh(catalog: dict[str, Any], fetch_stats: Callable[[str, str], tuple[str, str, dict[str, int]] | None], *,
            max_fetches: int = 25, ttl_hours: float = 24, now: datetime | None = None,
            only: Iterable[str] | None = None) -> dict[str, Any]:
    """Mutates `catalog` in place. Returns a summary."""
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=ttl_hours)
    only_set = {s.lower() for s in only} if only else None
    queue = []
    for s in catalog.get("skills") or []:
        if not is_clawhub_linked(s) or (only_set is not None and str(s.get("slug", "")).lower() not in only_set):
            continue
        due, age_key = _due(s.get("external_ratings") or {}, now, ttl)
        if due or only_set is not None:
            queue.append((age_key, str(s.get("slug") or ""), s))
    queue.sort(key=lambda t: (t[0], t[1]))
    summary: dict[str, Any] = {"linked_due": len(queue), "fetches": 0, "updated": [], "unresolved": [],
                               "url_fixed": [], "skipped_no_slug": [], "deferred": 0}
    stamp = iso(now)
    for _, sslug, s in queue:
        slug, owners = target(s)
        if not slug or not owners:
            summary["skipped_no_slug" if not slug else "unresolved"].append(sslug)
            if slug:
                er = s.setdefault("external_ratings", {})
                er["clawhub_stats_at"], er["clawhub_stats_status"] = stamp, "unresolved"
            continue
        if summary["fetches"] + 1 > max_fetches:
            summary["deferred"] += 1
            continue
        found = None
        for o in owners:
            if summary["fetches"] >= max_fetches:
                break
            summary["fetches"] += 1
            found = fetch_stats(o, slug)
            if found:
                break
        er = s.get("external_ratings")
        if not isinstance(er, dict):
            er = s["external_ratings"] = {}
        if not found:
            if summary["fetches"] >= max_fetches and owners and o != owners[-1]:
                summary["deferred"] += 1  # ran out of budget mid-candidates; retry next run
                continue
            er["clawhub_stats_at"], er["clawhub_stats_status"] = stamp, "unresolved"
            summary["unresolved"].append(sslug)
            continue
        owner, cslug, stats = found
        for k in STAT_KEYS:
            if isinstance(stats.get(k), int):
                er[f"clawhub_{k}"] = int(stats[k])
        canon = f"{C.BASE}/{owner}/skills/{cslug}"
        if er.get("clawhub_url") != canon:
            summary["url_fixed"].append({"slug": sslug, "from": er.get("clawhub_url"), "to": canon})
            er["clawhub_url"] = canon
        er["clawhub_stats_at"], er["clawhub_stats_status"] = stamp, "ok"
        summary["updated"].append({"slug": sslug, **{k: stats.get(k) for k in STAT_KEYS}})
    return summary


def make_fetcher(state_dir: Path = STATE) -> tuple[PageStats, DiskCache]:
    cache = DiskCache(state_dir / "clawhub-stats-robots.json")
    client = C.ClawHubClient(cache)
    client.load_robots()
    return PageStats(client), cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--apply", action="store_true", help="write the catalog in place (default: dry-run)")
    ap.add_argument("--max-fetches", type=int, default=25)
    ap.add_argument("--ttl-hours", type=float, default=24)
    ap.add_argument("--only", default=None, help="comma-separated catalog slugs (forces a refresh)")
    ap.add_argument("--state-dir", type=Path, default=STATE)
    args = ap.parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    ids_before = [(s.get("id"), s.get("slug"), s.get("category"), s.get("repo_url")) for s in catalog.get("skills") or []]
    args.state_dir.mkdir(parents=True, exist_ok=True)
    fetcher, cache = make_fetcher(args.state_dir)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    summary = refresh(catalog, fetcher, max_fetches=args.max_fetches, ttl_hours=args.ttl_hours, only=only)
    cache.save()
    ids_after = [(s.get("id"), s.get("slug"), s.get("category"), s.get("repo_url")) for s in catalog.get("skills") or []]
    assert ids_before == ids_after, "identity fields changed"
    if args.apply:
        args.catalog.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"apply": args.apply, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
