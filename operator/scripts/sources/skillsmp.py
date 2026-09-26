"""Adapter: SkillsMP (https://skillsmp.com), an index of GitHub-hosted agent skills.

Access (researched 2026-09-26, see operator/docs/sources.md "SkillsMP"):
* robots.txt on skillsmp.com: `User-agent: *` -> `Disallow: /api/` (and `/auth/`, `/api/github-contents`),
  `Crawl-delay: 1`. The REST search API lives under `/api/v1/...`, so this adapter never calls it.
* SkillsMP's official, documented MCP endpoint `POST https://skillsmp.com/mcp` (Streamable HTTP, JSON-RPC,
  read-only tools `search_skills` / `get_skill` / `list_categories`) is NOT disallowed by robots and is the
  channel SkillsMP recommends for agents. No API key or account is needed; no daily quota; limits are
  50 POSTs / 10 s and 30 `tools/call` / 60 s per client IP; 429 carries `Retry-After`.
* Terms of Service: browse/search allowed; "You may not scrape or systematically download large portions of
  the website"; every skill is subject to its GitHub repository's license. So each run is small and bounded
  (`max_calls` search calls, >= 2.5 s apart, results cached 6 h) and SkillsMP never supplies a license.

Identity:
* Listing with a GitHub `githubUrl` (`https://github.com/<owner>/<repo>/tree/<ref>/<path>`) -> a GitHub
  observation (source `skillsmp`, SkillsMP metrics as provenance only). It then goes through the normal
  GitHub checks (git tree SKILL.md discovery, `github:owner/repo::subpath` identity, catalog dedupe,
  repo license, mandatory static_scan), so it dedups against every other source and the catalog.
* Listing without a usable GitHub source -> record `source_type: skillsmp`, identity `skillsmp:<listing-id>`,
  status `missing_license`: SkillsMP gives no license evidence and no SKILL.md text to scan, so under the
  ClawHub-only rules (license must pass, static scan mandatory) it can never pass and is never filed.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote

from .base import DiskCache, iso, observation, parse_github_link
from .clawhub import robots_allows, robots_rules

NAME = "skillsmp"
BASE = "https://skillsmp.com"
MCP_PATH = "/mcp"
MCP_URL = f"{BASE}{MCP_PATH}"
ROBOTS_URL = f"{BASE}/robots.txt"
PROTOCOL_VERSION = "2025-06-18"
UA = "proskills-operator-sources/1.0 (+https://proskills.md)"
TOS_URL = f"{BASE}/terms"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,250}$")
MAX_LIMIT = 50  # MCP search_skills: limit <= 50, page <= 50
MAX_PAGE = 50

# (status, headers(lowercase keys), body)
Post = Callable[[str, bytes, dict], "tuple[int, dict, str]"]
Get = Callable[[str], "tuple[int, str]"]


class SkillsMPTransient(Exception):
    """Temporary failure (network, 5xx, 429): stop this run, retry next run (nothing cached)."""


class SkillsMPError(Exception):
    """Permanent failure for one request (4xx, JSON-RPC/tool error): skip that query."""


def default_post(url: str, body: bytes, headers: dict, timeout: float = 30.0) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, ""
    except Exception:
        return 0, {}, ""


def default_get(url: str, timeout: float = 30.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _jsonrpc_payload(text: str) -> dict[str, Any]:
    """Plain JSON, or the last `data:` event of an SSE (text/event-stream) response."""
    t = (text or "").strip()
    if t.startswith("{"):
        return json.loads(t)
    datas = [ln[5:].strip() for ln in t.splitlines() if ln.startswith("data:")]
    for d in reversed(datas):
        if d.startswith("{"):
            return json.loads(d)
    raise ValueError("no JSON-RPC payload")


class SkillsMPClient:
    """Tiny MCP (Streamable HTTP, JSON-RPC) client for the public SkillsMP server. Read-only tools only."""

    def __init__(self, cache: DiskCache, *, post: Post = default_post, get: Get = default_get,
                 min_interval: float = 2.5, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, search_ttl_s: float = 6 * 3600,
                 max_retry_wait_s: float = 30.0):
        self.cache = cache
        self.post = post
        self.get = get
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self.search_ttl_s = search_ttl_s
        self.max_retry_wait_s = max_retry_wait_s
        self._last: float | None = None
        self._id = 0
        self.session: str | None = None
        self.initialized = False
        self.requests = 0
        self.tool_calls = 0
        self.rules: list[tuple[str, str]] | None = None

    # -- robots
    def load_robots(self) -> list[tuple[str, str]]:
        cached = self.cache.get("skillsmp:robots", 86400)
        if cached is None:
            self._wait()
            self.requests += 1
            status, text = self.get(ROBOTS_URL)
            if status == 200:
                cached = text
                self.cache.put("skillsmp:robots", cached)
            elif 400 <= status < 500:
                cached = ""  # no robots.txt -> default allow (the /api/ guard below still applies)
                self.cache.put("skillsmp:robots", cached)
            else:
                raise SkillsMPTransient(f"robots.txt status {status}")
        self.rules = robots_rules(cached)
        return self.rules

    def mcp_allowed(self) -> bool:
        return robots_allows(self.rules or [], MCP_PATH)

    # -- transport
    def _wait(self) -> None:
        if self._last is not None:
            d = self.min_interval - (self.clock() - self._last)
            if d > 0:
                self.sleep(d)
        self._last = self.clock()

    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict[str, Any] | None:
        path = MCP_PATH
        if path.startswith("/api/") or (self.rules is not None and not robots_allows(self.rules, path)):
            raise PermissionError(f"robots.txt disallows {path}")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        headers = {"User-Agent": UA, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = json.dumps(msg).encode()
        for attempt in (1, 2):
            self._wait()
            self.requests += 1
            status, hdrs, text = self.post(MCP_URL, body, headers)
            if status in (200, 202):
                if hdrs.get("mcp-session-id") and not self.session:
                    self.session = str(hdrs["mcp-session-id"])[:200]
                if notify:
                    return None
                try:
                    data = _jsonrpc_payload(text)
                except (ValueError, json.JSONDecodeError) as e:
                    raise SkillsMPTransient(f"{method}: unparseable response ({e})")
                if data.get("error"):
                    raise SkillsMPError(f"{method}: rpc error {data['error'].get('code')}: "
                                        f"{str(data['error'].get('message'))[:120]}")
                return data.get("result") or {}
            if status == 429 or status == 0 or status >= 500:
                wait = 5.0
                if status == 429:
                    try:
                        wait = float(hdrs.get("retry-after") or 0) or 10.0
                    except ValueError:
                        wait = 10.0
                if attempt == 1 and wait <= self.max_retry_wait_s:
                    self.sleep(wait)
                    continue
                raise SkillsMPTransient(f"{method}: HTTP {status}")
            raise SkillsMPError(f"{method}: HTTP {status}")
        raise SkillsMPTransient(f"{method}: retries exhausted")  # pragma: no cover

    def initialize(self) -> None:
        if self.initialized:
            return
        self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                 "clientInfo": {"name": "proskills-operator", "version": "1.0"}})
        self._rpc("notifications/initialized", notify=True)
        self.initialized = True

    def search(self, query: str, *, sort: str = "recent", page: int = 1, limit: int = MAX_LIMIT,
               allow_fetch: bool = True) -> dict[str, Any] | None:
        """search_skills page -> {"skills": [...], "pagination": {...}} (cached); None when not cached and
        allow_fetch is False."""
        limit = max(1, min(MAX_LIMIT, int(limit)))
        page = max(1, min(MAX_PAGE, int(page)))
        key = f"skillsmp:search:{sort}:{limit}:{page}:{query}".lower()
        cached = self.cache.get(key, self.search_ttl_s)
        if cached is not None or not allow_fetch:
            return cached
        self.initialize()
        self.tool_calls += 1
        res = self._rpc("tools/call", {"name": "search_skills",
                                       "arguments": {"query": query, "sortBy": sort, "page": page, "limit": limit}})
        if res.get("isError"):
            raise SkillsMPError(f"search_skills error: {str((res.get('content') or [{}])[0].get('text'))[:120]}")
        data = None
        for c in res.get("content") or []:
            if c.get("type") == "text":
                try:
                    data = json.loads(c.get("text") or "")
                    break
                except json.JSONDecodeError:
                    continue
        if data is None and isinstance(res.get("structuredContent"), dict):
            data = res["structuredContent"]
        if not isinstance(data, dict):
            raise SkillsMPTransient("search_skills: no JSON content")
        slim = {"skills": [slim_listing(s) for s in data.get("skills") or [] if isinstance(s, dict)],
                "pagination": {k: (data.get("pagination") or {}).get(k) for k in ("page", "hasNext", "total")}}
        self.cache.put(key, slim)
        return slim


def slim_listing(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(s.get("id") or "")[:250], "name": str(s.get("name") or "")[:120],
            "author": str(s.get("author") or "")[:100], "description": str(s.get("description") or "")[:300],
            "githubUrl": str(s.get("githubUrl") or "")[:500] or None,
            "skillUrl": str(s.get("skillUrl") or "")[:500] or None,
            "stars": s.get("stars") if isinstance(s.get("stars"), int) else None,
            "updatedAt": s.get("updatedAt") if isinstance(s.get("updatedAt"), (int, float)) else None,
            "contentLanguage": str(s.get("contentLanguage") or "")[:10] or None}


# --------------------------------------------------------------------------- mapping / records

def skillsmp_identity(listing_id: str) -> str | None:
    lid = (listing_id or "").strip().lower()
    return f"skillsmp:{lid}" if ID_RE.match(lid) else None


def github_mapping(listing: dict[str, Any]) -> tuple[str, str, str | None] | None:
    u = listing.get("githubUrl")
    return parse_github_link(u) if u else None


def listing_url(listing: dict[str, Any]) -> str:
    u = str(listing.get("skillUrl") or "")
    if u.startswith(BASE + "/"):
        return u
    return f"{BASE}/search?q={quote(str(listing.get('name') or listing.get('id') or ''), safe='')}"


def skillsmp_catalog_index(catalog: dict[str, Any]) -> set[str]:
    """skillsmp.com URLs already in the catalog (normally none: SkillsMP-only skills never pass)."""
    out: set[str] = set()
    for s in catalog.get("skills") or []:
        for u in (s.get("repo_url"), (s.get("external_ratings") or {}).get("skillsmp_url")):
            if u and "skillsmp.com/" in str(u):
                out.add(str(u).split("?")[0].rstrip("/").lower())
    return out


def normalize_queries(queries: list[Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for q in queries or []:
        if isinstance(q, str):
            q = {"q": q}
        text = str(q.get("q") or "").strip()[:200]
        sort = q.get("sortBy") or q.get("sort") or "recent"
        if text and sort in ("recent", "stars") and (text, sort) not in out:
            out.append((text, sort))
    return out


def collect(client: SkillsMPClient, catalog: dict[str, Any], queries: list[Any], *, max_calls: int = 12,
            max_pages: int = 2, per_page: int = MAX_LIMIT, min_stars: int = 0, rotation: int = 0,
            now_iso: str | None = None) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Returns (github_observations, skillsmp_only_records, info). Bounded: at most `max_calls` uncached
    search_skills calls per run; cached pages are free. A temporary error stops the run (retry next run).
    `rotation` shifts the query plan (source_candidates passes the UTC hour) so no query starves under the cap."""
    info: dict[str, Any] = {"robots_allows_mcp": None, "api_allowed": None, "queries": 0, "calls": 0,
                            "pages_cached": 0, "pages_deferred": 0, "listings_seen": 0, "duplicate_listings": 0,
                            "github_backed_listings": 0, "skillsmp_only_listings": 0, "below_min_stars": 0,
                            "errors": []}
    now_iso = now_iso or iso()
    try:
        rules = client.load_robots()
    except SkillsMPTransient as e:
        info["error"] = f"transient:{e}"
        return [], [], info
    info["robots_allows_mcp"] = robots_allows(rules, MCP_PATH)
    info["api_allowed"] = robots_allows(rules, "/api/v1/skills/search")
    if not info["robots_allows_mcp"]:
        info["error"] = "mcp_disallowed_by_robots"
        return [], [], info
    plan = normalize_queries(queries)
    if plan:
        r = int(rotation) % len(plan)
        plan = plan[r:] + plan[:r]
    info["queries"] = len(plan)
    listed = skillsmp_catalog_index(catalog)
    seen: set[str] = set()
    obs: list[dict] = []
    records: list[dict] = []
    stopped = None
    for text, sort in plan:
        for page in range(1, max(1, min(MAX_PAGE, int(max_pages))) + 1):
            data = client.search(text, sort=sort, page=page, limit=per_page, allow_fetch=False)
            if data is not None:
                info["pages_cached"] += 1
            elif stopped or info["calls"] >= max_calls:
                info["pages_deferred"] += 1
                break
            else:
                info["calls"] += 1
                try:
                    data = client.search(text, sort=sort, page=page, limit=per_page)
                except SkillsMPTransient as e:
                    stopped = f"transient:{e}"
                    info["pages_deferred"] += 1
                    break
                except SkillsMPError as e:
                    info["errors"].append(f"{text!r}/{sort} p{page}: {e}")
                    break
            for s in data.get("skills") or []:
                lid = str(s.get("id") or "")
                if not lid:
                    continue
                if lid in seen:
                    info["duplicate_listings"] += 1
                    continue
                seen.add(lid)
                info["listings_seen"] += 1
                stars = s.get("stars")
                if min_stars and (stars or 0) < min_stars:
                    info["below_min_stars"] += 1
                    continue
                url = listing_url(s)
                metrics = {"skillsmp_id": lid, "skillsmp_stars": stars, "skillsmp_updated_at": s.get("updatedAt"),
                           "skillsmp_query": f"{sort}:{text}", "content_language": s.get("contentLanguage")}
                gh = github_mapping(s)
                if gh:
                    o, r, sub = gh
                    info["github_backed_listings"] += 1
                    obs.append(observation(o, r, NAME, url, subpath=sub, observed_at=now_iso,
                                           metrics={**metrics, "mapping": "githubUrl"}))
                    continue
                ident = skillsmp_identity(lid)
                if not ident:
                    info["errors"].append(f"unusable listing id {lid[:60]!r}")
                    continue
                info["skillsmp_only_listings"] += 1
                rec: dict[str, Any] = {
                    "identity": ident, "source_type": NAME, "issue": None, "skillsmp_id": lid,
                    "owner": (s.get("author") or "").lower() or None, "slug": (s.get("name") or "").lower() or None,
                    "repo_url": url, "source_url": url, "commit_sha": None,
                    "license_spdx": None, "license": None, "license_tier": None,
                    "license_evidence": ["skillsmp_no_license_evidence",
                                         f"skillsmp_terms:{TOS_URL} (skills are subject to their repository's license)"],
                    "skill_path": None, "subpath": None, "stars": None, "forks": None,
                    "created_at": None, "pushed_at": None, "skill_name": s.get("name") or None,
                    "skill_description": s.get("description") or None,
                    "sources": [{"source": NAME, "source_url": url, "observed_at": now_iso, "metrics": metrics}],
                    "existing_issue": None, "repo_has_open_issue": False,
                    "lane": NAME, "score": 0.0, "multi_source_count": 1,
                }
                if url.split("?")[0].rstrip("/").lower() in listed:
                    rec.update({"status": "already_in_catalog", "catalog_reason": "catalog_skillsmp_url"})
                else:
                    # ClawHub-only rules: license must pass + static scan mandatory. SkillsMP supplies neither a
                    # license nor the SKILL.md text, so a SkillsMP-only listing is never filed.
                    rec.update({"status": "missing_license", "hold": "skillsmp_only_no_github_source"})
                records.append(rec)
            if not (data.get("pagination") or {}).get("hasNext"):
                break
    if stopped:
        info["error"] = stopped
    info["calls_made"] = client.tool_calls
    info["requests"] = client.requests
    info["github_mapped"] = len(obs)
    info["skillsmp_only_records"] = len(records)
    info["errors"] = info["errors"][:20]
    return obs, records, info
