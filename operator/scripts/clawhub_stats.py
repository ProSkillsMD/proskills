#!/usr/bin/env python3
"""Refresh ClawHub download / install / star counts for ClawHub-linked catalog skills.

Data source and compliance
--------------------------
ClawHub's robots.txt has `User-agent: * / Disallow: /api/`. That rule is aimed at crawlers and indexers. ClawHub
also documents a public read API for exactly this use: docs/api.md and docs/http-api.md in openclaw/clawhub,
"Public catalog reuse". Third-party directories "may use the public read endpoints" (`GET /api/v1/skills/{slug}`,
`GET /api/v1/search`) provided they cache results, honour `429` / `Retry-After` and rate-limit headers, link back
to the canonical ClawHub listing, and do not imply endorsement. This script does targeted lookups of skills we
already list, not crawling: at most `--max-requests` per run (default 60, against a documented 3000/min anonymous
limit), >= 1 s apart, cached, with bounded retries. So it uses the documented API and nothing else:
  * GET /api/v1/skills/{slug}      stats + owner for a unique slug (200), 404 = not on ClawHub, 409 = ambiguous slug
  * GET /api/v1/search?q={slug}    only to pick the right owner when the slug is ambiguous or the owner differs
No HTML pages are scraped here, and no other /api/ path is called (enforced in `ApiClient._get`).
Docs: https://github.com/openclaw/clawhub/blob/main/docs/api.md
      https://github.com/openclaw/clawhub/blob/main/docs/http-api.md  (section "Public catalog reuse")

Request accounting: every HTTP attempt, including retries, counts against `--max-requests` and is paced >= 1 s
after the previous attempt. `Retry-After` (delta-seconds or HTTP-date) is honoured exactly: if it asks us to wait
longer than the cap (60 s) or than the time left in the run, we do not sleep past it and do not retry early; the run
stops making requests (remaining skills are deferred untouched) and the current skill is recorded as fetch_failed.

Catalog fields (append-only, inside `external_ratings`)
-------------------------------------------------------
  clawhub_stats_status     "ok" | "not_found" | "fetch_failed"
  clawhub_stats_reason     short machine reason, e.g. "http_404", "owner_mismatch", "http_429", "timeout"
  clawhub_fetched_at       ISO-8601 UTC time of the last attempt (any outcome)
  clawhub_last_success_at  ISO-8601 UTC time the counts below were last read successfully (only set on "ok")
  clawhub_downloads / clawhub_installs / clawhub_stars / clawhub_comments   written ONLY on "ok"
  clawhub_url              canonical listing URL returned by ClawHub, written only on "ok"
A failed or not-found refresh never writes counts and never touches clawhub_last_success_at, so the last good
values and their retrieval time survive. Counts WITHOUT a dated successful read (legacy rows, 0/null placeholders)
are removed by `sanitize_public()` on every run, because the catalog is served publicly as-is. Consumers show them as "last known <date>", or as unavailable when there
is no clawhub_last_success_at. id, slug, category, repo_url and every other top-level field are never modified.

Scheduling: the catalog is the cache. "ok" and "not_found" are refreshed after --ttl-hours (24). "fetch_failed" is
retried after 1 h (ClawHub was temporarily unreachable). Never-checked records go first, then the stalest. A per-run
request cap spreads the work. HTTP responses are also cached on disk for --cache-ttl-min (60) within/between runs.

Usage (dry-run by default):
  python3 operator/scripts/clawhub_stats.py --catalog <website>/public/skills-catalog.json [--apply]
      [--max-requests 60] [--ttl-hours 24] [--only slug1,slug2]
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.base import STATE, DiskCache, iso, parse_iso  # noqa: E402

BASE = "https://clawhub.ai"
UA = "proskills-operator-clawhub-stats/1.0 (+https://proskills.md)"
STAT_KEYS = ("downloads", "installs", "stars", "comments")
# Every ClawHub count field a catalog row may carry (clawhub_rating = legacy name for stars).
COUNT_FIELDS = ("clawhub_downloads", "clawhub_installs", "clawhub_stars", "clawhub_rating", "clawhub_comments")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
RESERVED_OWNERS = frozenset({"skills", "plugins", "official", "docs", "api", "admin", "search", "u", "clawhub"})
# Documented public read endpoints, see https://github.com/openclaw/clawhub/blob/main/docs/http-api.md
# ("Public catalog reuse") and https://github.com/openclaw/clawhub/blob/main/docs/api.md
ALLOWED_PATHS = (re.compile(r"^/api/v1/skills/[A-Za-z0-9_.-]+$"), re.compile(r"^/api/v1/search$"))
FAILED_RETRY = timedelta(hours=1)
TRANSIENT = "fetch_failed"

# (status, headers, body). status 0 = network error / timeout.
Fetch = Callable[[str], "tuple[int, dict[str, str], str]"]


def default_fetch(url: str, timeout: float = 20.0) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
    except Exception:  # noqa: BLE001 - timeouts, DNS, resets
        return 0, {}, ""


def is_transient(status: int) -> bool:
    return status == 0 or status == 429 or status >= 500


@dataclass
class Result:
    status: str                      # ok | not_found | fetch_failed
    reason: str = ""
    owner: str | None = None
    url: str | None = None
    stats: dict[str, int] = field(default_factory=dict)


class ApiClient:
    """Documented ClawHub public read API only (openclaw/clawhub docs/api.md + docs/http-api.md, "Public catalog
    reuse"). Polite: every attempt (retries included) is paced and counted against the per-run budget, bounded
    retries with exponential backoff + jitter, Retry-After honoured (seconds or HTTP-date) and never undercut,
    wall-clock deadline, definitive responses cached on disk."""

    def __init__(self, cache: DiskCache | None, *, fetch: Fetch = default_fetch, min_interval: float = 1.0,
                 max_attempts: int = 3, backoff_base: float = 2.0, backoff_cap: float = 30.0,
                 retry_after_cap: float = 60.0, max_requests: int = 60, cache_ttl_s: float = 3600,
                 deadline_s: float = 180.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[], float] = random.random,
                 wallclock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cache, self.fetch = cache, fetch
        self.min_interval, self.max_attempts = min_interval, max_attempts
        self.backoff_base, self.backoff_cap, self.retry_after_cap = backoff_base, backoff_cap, retry_after_cap
        self.max_requests, self.cache_ttl_s = max_requests, cache_ttl_s
        self.sleep, self.clock, self.rand, self.wallclock = sleep, clock, rand, wallclock
        self.deadline_s = deadline_s  # wall-clock budget per run, so a slow ClawHub can never stall a publish
        self.requests = 0            # HTTP attempts made, retries included
        self.attempt_times: list[float] = []
        self.sleeps: list[float] = []
        self.halted: str | None = None  # set when ClawHub asks us to back off beyond what this run can wait
        self._last: float | None = None
        self._started = clock()

    def time_left(self) -> float:
        return self.deadline_s - (self.clock() - self._started)

    def budget_left(self) -> int:
        if self.halted or self.time_left() <= 0:
            return 0
        return max(0, self.max_requests - self.requests)

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(round(seconds, 3))
            self.sleep(seconds)

    def retry_after_seconds(self, value: str | None) -> float | None:
        """Retry-After as seconds from now. Accepts delta-seconds ("7") and HTTP-date
        ("Sun, 27 Sep 2026 14:40:00 GMT"). None when absent or unparseable."""
        if not value:
            return None
        v = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", v):
            return float(v)
        try:
            when = parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - self.wallclock()).total_seconds())

    def backoff(self, attempt: int, retry_after: str | None = None) -> float:
        """Delay before retry `attempt` (1-based): Retry-After if given (uncapped; the caller decides whether it
        can wait that long), else exponential backoff with equal jitter in [exp/2, exp]."""
        ra = self.retry_after_seconds(retry_after)
        if ra is not None:
            return ra
        exp = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return exp / 2 + self.rand() * exp / 2

    def _get(self, path: str, query: dict[str, str] | None = None) -> tuple[int, str]:
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise PermissionError(f"not an allowed documented read endpoint: {path}")
        url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        key = f"clawhub-api:{url}"
        if self.cache is not None:
            hit = self.cache.get(key, self.cache_ttl_s)
            if hit is not None:
                return int(hit["status"]), str(hit["body"])
        status, body = -1, ""
        for attempt in range(1, self.max_attempts + 1):
            if self.budget_left() <= 0:
                break  # request/time budget used up (retries count too); a prior failed attempt stays failed
            if self._last is not None:
                pace = self.min_interval - (self.clock() - self._last)
                if pace >= self.time_left():
                    break
                self._wait(pace)  # 1 req/s applies to every attempt, retries included
            self._last = self.clock()
            self.attempt_times.append(self._last)
            self.requests += 1
            status, headers, body = self.fetch(url)
            if not is_transient(status):
                break
            if attempt >= self.max_attempts:
                break
            ra = self.retry_after_seconds(headers.get("retry-after"))
            delay = self.backoff(attempt, headers.get("retry-after"))
            if ra is not None and (ra > self.retry_after_cap or ra >= self.time_left()):
                # ClawHub asked for a longer pause than this run can honour: stop calling ClawHub for this run
                # instead of sleeping past our budget or retrying early.
                self.halted = f"retry_after_{int(ra)}s"
                break
            if delay >= self.time_left() or self.max_requests - self.requests <= 0:
                break  # no time or no request budget left for another attempt
            self._wait(delay)
        if self.cache is not None and status > 0 and not is_transient(status):
            self.cache.put(key, {"status": status, "body": body})  # only definitive answers are cached
        return status, body

    # ------------------------------------------------------------------ lookups

    def skill(self, slug: str, owner: str | None) -> Result:
        status, body = self._get(f"/api/v1/skills/{slug}")
        if status == -1:
            return Result("deferred", "budget")
        if is_transient(status):
            return Result(TRANSIENT, "timeout" if status == 0 else f"http_{status}")
        if status == 404:
            return Result("not_found", "http_404")
        if status == 200:
            try:
                d = json.loads(body)
                got = str((d.get("owner") or {}).get("handle") or "")
                stats = {k: int(v) for k, v in ((d.get("skill") or {}).get("stats") or {}).items()
                         if k in STAT_KEYS and isinstance(v, int) and v >= 0}
            except (ValueError, TypeError, AttributeError):
                return Result(TRANSIENT, "bad_json")
            if not got or "downloads" not in stats:
                return Result(TRANSIENT, "incomplete_payload")
            if owner is None or got.lower() == owner.lower():
                return Result("ok", "", got, f"{BASE}/{got}/skills/{slug}", stats)
            return self._search(slug, owner, fallback_reason="owner_mismatch")
        if status == 409:
            if owner is None:
                return Result("not_found", "ambiguous_slug_no_owner")
            return self._search(slug, owner, fallback_reason="ambiguous_no_owner_match")
        return Result("not_found" if 400 <= status < 500 else TRANSIENT, f"http_{status}")

    def _search(self, slug: str, owner: str, *, fallback_reason: str) -> Result:
        status, body = self._get("/api/v1/search", {"q": slug})
        if status == -1:
            return Result("deferred", "budget")
        if status != 200:
            return Result(TRANSIENT if is_transient(status) else "not_found", f"search_http_{status}")
        try:
            results = json.loads(body).get("results") or []
        except (ValueError, AttributeError):
            return Result(TRANSIENT, "search_bad_json")
        for r in results:
            native = r.get("native") or {}
            sk = native.get("skill") or {}
            handle = str(native.get("ownerHandle") or "")
            if sk.get("slug") == slug and handle.lower() == owner.lower():
                stats = {k: int(v) for k, v in (sk.get("stats") or {}).items()
                         if k in STAT_KEYS and isinstance(v, int) and v >= 0}
                if "downloads" not in stats:
                    return Result(TRANSIENT, "search_incomplete_payload")
                canon = r.get("canonicalUrl") or f"/{handle}/skills/{slug}"
                return Result("ok", "", handle, BASE + canon if canon.startswith("/") else canon, stats)
        return Result("not_found", fallback_reason)


# ---------------------------------------------------------------------- catalog side

def _clawhub_urls(skill: dict[str, Any]) -> list[str]:
    er = skill.get("external_ratings") or {}
    out = []
    for u in (er.get("clawhub_url"), skill.get("repo_url"), skill.get("source_url"), skill.get("author_url")):
        if isinstance(u, str) and "clawhub.ai/" in u and u not in out:
            out.append(u)
    return out


def parse_clawhub_url(url: str) -> tuple[str | None, str | None]:
    """(owner|None, slug|None) from any ClawHub skill URL form seen in the catalog."""
    path = url.split("clawhub.ai", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None, None
    if len(parts) >= 3 and parts[-2] == "skills":          # /owner/skills/slug
        owner, slug = parts[-3].lstrip("@"), parts[-1]
    elif len(parts) == 2 and parts[0] == "skills":          # /skills/slug (owner unknown)
        owner, slug = None, parts[1]
    elif len(parts) == 2:                                  # /owner/slug or /@owner/slug
        owner, slug = parts[0].lstrip("@"), parts[1]
    else:
        return None, None
    if owner and (not NAME_RE.match(owner) or owner.lower() in RESERVED_OWNERS):
        owner = None
    if not NAME_RE.match(slug) or slug.lower() in RESERVED_OWNERS:
        return owner, None
    return owner, slug


def is_clawhub_linked(skill: dict[str, Any]) -> bool:
    return skill.get("source_type") == "clawhub" or bool(_clawhub_urls(skill))


def target(skill: dict[str, Any]) -> tuple[str | None, str | None]:
    """(clawhub slug, owner from a ClawHub URL or None). Only URL-derived owners are trusted: guessing from
    author/GitHub names could attach another publisher's numbers to our listing."""
    slug = owner = None
    for u in _clawhub_urls(skill):
        o, s = parse_clawhub_url(u)
        slug = slug or s
        owner = owner or o
    return slug, owner


def _legacy_migrate(er: dict[str, Any]) -> None:
    """Earlier draft of this PR wrote clawhub_stats_at / status "unresolved"; fold them into the new fields."""
    at = er.pop("clawhub_stats_at", None)
    if er.get("clawhub_stats_status") == "unresolved":
        er.pop("clawhub_stats_status", None)
    elif er.get("clawhub_stats_status") == "ok" and at and not er.get("clawhub_last_success_at"):
        er["clawhub_last_success_at"] = at
        er.setdefault("clawhub_fetched_at", at)


def has_dated_success(er: dict[str, Any]) -> bool:
    return er.get("clawhub_stats_status") in ("ok", "not_found", TRANSIENT) and \
        parse_iso(er.get("clawhub_last_success_at")) is not None


def sanitize_public(catalog: dict[str, Any]) -> dict[str, int]:
    """Drop ClawHub counts that have no dated, verified source (no clawhub_last_success_at from a successful API
    read). The catalog is published as-is at proskills.md/skills-catalog.json, so undated legacy numbers (and the
    0 / null placeholders older rows carry) must not be in it. They are dropped, not archived: their provenance and
    retrieval date are unknown, so there is nothing trustworthy to keep. Dated counts (last good values) are
    never touched. Returns {"rows": n, "fields": m}."""
    rows = fields = 0
    for s in catalog.get("skills") or []:
        er = s.get("external_ratings")
        if not isinstance(er, dict) or has_dated_success(er):
            continue
        hit = [k for k in COUNT_FIELDS if k in er]
        for k in hit:
            del er[k]
        if hit:
            rows, fields = rows + 1, fields + len(hit)
    return {"rows": rows, "fields": fields}


def _due(er: dict[str, Any], now: datetime, ttl: timedelta) -> tuple[bool, float]:
    at = parse_iso(er.get("clawhub_fetched_at"))
    if at is None:
        return True, 0.0
    wait = FAILED_RETRY if er.get("clawhub_stats_status") == TRANSIENT else ttl
    return now - at >= wait, at.timestamp()


def refresh(catalog: dict[str, Any], client: ApiClient, *, ttl_hours: float = 24, now: datetime | None = None,
            only: Iterable[str] | None = None) -> dict[str, Any]:
    """Mutates `catalog` in place (append-only external_ratings fields). Returns a summary. Never raises for
    ClawHub errors: those are recorded per skill as fetch_failed."""
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=ttl_hours)
    only_set = {s.lower() for s in only} if only else None
    queue = []
    for s in catalog.get("skills") or []:
        if not is_clawhub_linked(s) or (only_set is not None and str(s.get("slug", "")).lower() not in only_set):
            continue
        er = s.get("external_ratings")
        if isinstance(er, dict):
            _legacy_migrate(er)
        due, age_key = _due(er if isinstance(er, dict) else {}, now, ttl)
        if due or only_set is not None:
            queue.append((age_key, str(s.get("slug") or s.get("id") or ""), s))
    queue.sort(key=lambda t: (t[0], t[1]))
    summary: dict[str, Any] = {"due": len(queue), "ok": [], "not_found": [], "fetch_failed": [], "deferred": 0,
                               "url_fixed": 0}
    stamp = iso(now)
    for _, sslug, s in queue:
        slug, owner = target(s)
        er = s.get("external_ratings")
        if not isinstance(er, dict):
            er = s["external_ratings"] = {}
        if not slug:
            er.update(clawhub_stats_status="not_found", clawhub_stats_reason="no_clawhub_slug", clawhub_fetched_at=stamp)
            summary["not_found"].append(sslug)
            continue
        if client.budget_left() <= 0:
            summary["deferred"] += 1
            continue
        res = client.skill(slug, owner)
        if res.status == "deferred":
            summary["deferred"] += 1
            continue
        er["clawhub_fetched_at"] = stamp
        er["clawhub_stats_status"] = res.status
        er["clawhub_stats_reason"] = res.reason
        if res.status != "ok":
            summary[res.status].append(sslug)
            continue  # counts, clawhub_url and clawhub_last_success_at stay exactly as they were
        for k in STAT_KEYS:
            if k in res.stats:
                er[f"clawhub_{k}"] = res.stats[k]
        if "stars" in res.stats:
            er.pop("clawhub_rating", None)  # legacy undated name for stars, superseded by the dated value
        er["clawhub_last_success_at"] = stamp
        if res.url and er.get("clawhub_url") != res.url:
            er["clawhub_url"] = res.url
            summary["url_fixed"] += 1
        summary["ok"].append(sslug)
    summary["sanitized"] = sanitize_public(catalog)
    summary["requests"] = client.requests  # HTTP attempts, retries included
    summary["halted"] = client.halted
    return summary


def make_client(state_dir: Path = STATE, *, max_requests: int = 60,
                cache_ttl_s: float = 3600) -> tuple[ApiClient, DiskCache]:
    state_dir.mkdir(parents=True, exist_ok=True)
    cache = DiskCache(state_dir / "clawhub-stats-api-cache.json")
    return ApiClient(cache, max_requests=max_requests, cache_ttl_s=cache_ttl_s), cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--apply", action="store_true", help="write the catalog in place (default: dry-run)")
    ap.add_argument("--max-requests", type=int, default=60)
    ap.add_argument("--ttl-hours", type=float, default=24)
    ap.add_argument("--cache-ttl-min", type=float, default=60)
    ap.add_argument("--only", default=None, help="comma-separated catalog slugs (forces a refresh)")
    ap.add_argument("--state-dir", type=Path, default=STATE)
    args = ap.parse_args(argv)
    raw = args.catalog.read_text(encoding="utf-8")
    catalog = json.loads(raw)
    ident = lambda c: [(s.get("id"), s.get("slug"), s.get("category"), s.get("repo_url")) for s in c.get("skills") or []]  # noqa: E731
    before = ident(catalog)
    client, cache = make_client(args.state_dir, max_requests=args.max_requests, cache_ttl_s=args.cache_ttl_min * 60)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    summary = refresh(catalog, client, ttl_hours=args.ttl_hours, only=only)
    cache.save()
    assert ident(catalog) == before, "identity fields changed"
    if args.apply:
        args.catalog.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"apply": args.apply, **{k: (v if not isinstance(v, list) else {"count": len(v), "slugs": v})
                                              for k, v in summary.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
