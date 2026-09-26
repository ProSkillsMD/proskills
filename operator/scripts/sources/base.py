"""Shared pieces for source adapters: observations, disk cache, search throttle."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse

SCRIPTS = Path(__file__).resolve().parents[1]
OPS = SCRIPTS.parents[1]
CONFIG = OPS / "operator" / "config" / "sources.json"
STATE = OPS / "operator" / "state" / "sources"

GH_RESERVED = frozenset({
    "topics", "sponsors", "orgs", "features", "marketplace", "apps", "about", "pricing", "login", "join",
    "settings", "notifications", "explore", "collections", "trending", "search", "site", "security",
    "enterprise", "customer-stories", "readme", "events", "github-copilot", "codespaces", "new",
})
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
GH_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[^\s)\]\"'<>`]+", re.I)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def observation(owner: str, repo: str, source: str, source_url: str, *, subpath: str | None = None,
                metrics: dict[str, Any] | None = None, observed_at: str | None = None) -> dict[str, Any]:
    """One sighting of a repo (optionally a subfolder) in one source."""
    return {"owner": owner.lower(), "repo": repo.lower(), "subpath": (subpath or "").strip("/") or None,
            "source": source, "source_url": source_url, "observed_at": observed_at or iso(),
            "metrics": dict(metrics or {})}


def parse_github_link(url: str) -> tuple[str, str, str | None] | None:
    """(owner, repo, subpath|None) for a github.com repo link; None for non-repo pages.

    tree/<ref>/<path> and blob/<ref>/<path> keep <path> (a SKILL.md/README.md blob -> its folder).
    Other deep links (issues, pulls, wiki, releases ...) map to the repo itself.
    """
    try:
        p = urlparse(url.strip().rstrip(".,;:"))
    except ValueError:
        return None
    if (p.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner.lower() in GH_RESERVED or not NAME_RE.match(owner) or not NAME_RE.match(repo) or repo in (".", ".."):
        return None
    sub = None
    if len(parts) >= 5 and parts[2] in ("tree", "blob"):
        path = "/".join(parts[4:])
        if parts[2] == "blob":  # a file (SKILL.md, README.md, ...) -> its folder
            path = path.rsplit("/", 1)[0] if "/" in path else ""
        sub = path.strip("/") or None
    return owner, repo, sub


def github_links(text: str) -> Iterable[str]:
    seen: set[str] = set()
    for m in GH_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:*_")
        if u not in seen:
            seen.add(u)
            yield u


# --------------------------------------------------------------------------- disk cache

class DiskCache:
    """Tiny JSON cache: key -> {at, value}. TTL checked on read. Not thread-safe across processes."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time, autosave_every: int = 0):
        self.path = path
        self.clock = clock
        self.autosave_every = int(autosave_every or 0)  # >0: save after every N puts (survives a killed run)
        self._puts = 0
        self._lock = threading.Lock()
        try:
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str, ttl_s: float) -> Any | None:
        with self._lock:
            e = self.data.get(key)
            if e and self.clock() - float(e.get("at") or 0) <= ttl_s:
                self.hits += 1
                return e.get("value")
            self.misses += 1
            return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = {"at": self.clock(), "value": value}
            self._puts += 1
            due = self.autosave_every > 0 and self._puts % self.autosave_every == 0
        if due:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- search

class SearchLimiter:
    """GitHub search API: 30 requests/min authenticated -> at least `min_interval` s between calls."""

    def __init__(self, min_interval: float = 2.1, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None
        self.calls = 0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last is not None:
                delay = self.min_interval - (now - self._last)
                if delay > 0:
                    self.sleep(delay)
                    now = self.clock()
            self._last = now
            self.calls += 1


class RepoSearch:
    """Paged, cached, throttled repository search (respects the 1000-results-per-query cap)."""

    MAX_RESULTS = 1000

    def __init__(self, client: Any, cache: DiskCache, limiter: SearchLimiter, ttl_s: float = 6 * 3600):
        self.client = client
        self.cache = cache
        self.limiter = limiter
        self.ttl_s = ttl_s
        self.errors: list[str] = []

    def search(self, q: str, *, sort: str = "stars", max_pages: int = 3, per_page: int = 100) -> list[dict[str, Any]]:
        per_page = max(1, min(100, per_page))
        max_pages = max(1, min(max_pages, self.MAX_RESULTS // per_page))
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            path = (f"search/repositories?q={quote(q, safe='')}&sort={sort}&order=desc"
                    f"&per_page={per_page}&page={page}")
            cached = self.cache.get(f"search:{path}", self.ttl_s)
            if cached is None:
                self.limiter.wait()
                try:
                    data = self.client.rest(path) or {}
                except Exception as e:  # classified errors from scout.GitHubClient
                    self.errors.append(f"{q!r} p{page}: {type(e).__name__}: {e}")
                    break
                cached = {"total_count": int(data.get("total_count") or 0),
                          "items": [slim_repo(it) for it in data.get("items") or []]}
                self.cache.put(f"search:{path}", cached)
            items = cached.get("items") or []
            out.extend(items)
            total = int(cached.get("total_count") or 0)
            if len(items) < per_page or page * per_page >= min(total, self.MAX_RESULTS):
                break
        return out


def slim_repo(it: dict[str, Any]) -> dict[str, Any]:
    lic = it.get("license") or {}
    return {"full_name": it.get("full_name"), "html_url": it.get("html_url"),
            "stars": int(it.get("stargazers_count") or 0), "forks": int(it.get("forks_count") or 0),
            "created_at": it.get("created_at"), "pushed_at": it.get("pushed_at"), "fork": bool(it.get("fork")),
            "archived": bool(it.get("archived")), "license_spdx": lic.get("spdx_id"),
            "topics": list(it.get("topics") or [])[:20], "default_branch": it.get("default_branch")}
