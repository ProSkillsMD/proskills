#!/usr/bin/env python3
"""ProSkills committed intake scout (stdlib only; never executes candidate code).

Replaces the untracked, hourly-regenerated ``/tmp/intake_enrich_HHMM.py`` with a
tested module. Fixes vs. the old scout:

1. Committed + unit tested; GitHub access goes through an injectable transport
   so tests never touch the network.
2. Covers ALL open issues. Issue listing + repo metadata (GraphQL, 50 repos per
   query) run for every open issue on every run; the expensive part (recursive
   git tree + SKILL.md fetches) runs over a rotating window whose cursor is
   persisted in ``operator/state/scout/``. Per-repo verdicts are cached keyed to
   the repo's ``pushed_at`` so unchanged repos are never re-fetched.
3. Failures are split: ``not_found`` (404/410 / GraphQL NOT_FOUND) vs
   ``transient`` (403 rate limit, 429, 5xx, timeouts). Transient calls retry with
   backoff honouring ``Retry-After`` / ``X-RateLimit-Reset`` and the worker pool
   shrinks when rate limited. Transient outcomes are never cached or reported as
   rejections; those repos are retried first on the next run.
4. SKILL.md discovery uses the recursive git tree API (any depth: root,
   ``skills/*``, ``.claude/skills/*``, ``.agents/skills/*`` ...), capped per repo.
5. Identity is per skill folder: ``github:owner/repo`` for a root SKILL.md,
   ``github:owner/repo::subpath`` otherwise. Catalog dedupe works at subpath
   level; a whole-repo catalog entry blocks the root skill (and the only skill of
   a single-skill repo, and every skill of an ``is_collection`` listing) but not
   distinct subfolder skills of a multi-skill repo.
6. Three license tiers: ``pass`` (repo SPDX id), ``license_review`` (NOASSERTION,
   unclassified license file, per-skill LICENSE, or ``license:`` frontmatter;
   recorded with evidence, never put in ``passed``), reject (no evidence).
7. static_scan stays mandatory for every queued skill; critical findings hold.

Default mode is ``--dry-run`` (writes only ``scout-dryrun-*`` artifacts + the
scout cache). ``--write`` produces the same artifact set the /tmp scout wrote
(candidate-queue-YYYY-MM-DD.json and intake-*-{summary,eligible,scan-results,
batch-candidates,parent-result}.json). The scout never stages, publishes,
merges, labels or messages anything.
"""
from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
OPS = SCRIPTS.parents[1]
ART = OPS / "operator" / "state" / "artifacts"
LOGS = OPS / "operator" / "logs"
LOCKS = OPS / "operator" / "state" / "locks"
SCOUT_STATE = OPS / "operator" / "state" / "scout"
sys.path.insert(0, str(SCRIPTS))

from publish_lib import (  # noqa: E402
    LIVE_CATALOG_URL,
    PROTECTED_ISSUES,
    extract_github_urls,
    issue_is_blocked,
    parse_github_source,
    parse_skill_frontmatter,
)
from static_scan import scan_candidate  # noqa: E402

if __name__ not in sys.modules:
    # Loaded via importlib.util.spec_from_file_location(...).loader.exec_module() without registering
    # the module first: @dataclass (with postponed annotations) looks the module up in sys.modules and
    # crashes on None. Register a namespace shim so that loading style keeps working. Preferred loading
    # from a routine is simply `sys.path.insert(0, ".../operator/scripts"); import scout`.
    import types as _types
    _shim = _types.ModuleType(__name__)
    _shim.__dict__.update(globals())
    sys.modules[__name__] = _shim

REPO_SLUG = "ProSkillsMD/proskills"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DHAKA = timezone(timedelta(hours=6))
SOFT_CAP = 100
MATERIALIZE = "github_api_tree_raw_no_git_clone"
UA = "proskills-operator-scout/1.0"

# Holds that must always be respected (in addition to PROTECTED_ISSUES).
STATIC_HOLDS = frozenset({2396, 4869})          # critical_static
CREDENTIAL_LIKE_ISSUES = frozenset({2833})      # redacted title, publisher skip
KNOWN_HOLDS_FILES = ("candidate-queue-2026-09-25.pre-scan.json",)

DEFAULTS = {
    "max_skills_per_repo": 25,      # skills evaluated per issue/repo (root first, then shallow)
    "large_repo_threshold": 50,     # repos with more SKILL.md files are routed to large_collection review
    "max_tree_fetches": 600,        # rotating window: repos tree-fetched per run
    "min_core_remaining": 1500,     # stop tree fetches when REST core quota drops below this
    "workers": 8,
    "queue_max_passed": 40,         # same trim as the old scout
    "queue_max_per_repo": 5,        # keep a single monorepo from flooding `passed`
    "cache_max_age_days": 30,
    "graphql_batch": 50,
}

EXCLUDED_SEGMENTS = frozenset({
    "node_modules", "vendor", "third_party", "third-party", ".git", "dist", "build",
    "site-packages", "__pycache__", "fixtures", "__fixtures__", "testdata", ".venv", "venv",
})
LICENSE_NAME_RE = re.compile(r"^(licen[cs]e|copying|unlicense)([._-].*)?$", re.I)
NAME_OK_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
CRED_TITLE_RE = re.compile(r"(api[_-]?key|secret|token|password|passwd|credential)\s*[:=]", re.I)
SCAN_TEXT_EXT = (".md", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".json",
                 ".yaml", ".yml", ".toml", ".txt", ".ps1", ".rb", ".go", ".rs")
ROOT_SCAN_EXTRAS = ("README.md", "LICENSE", "LICENSE.md", "package.json", "pyproject.toml")


# --------------------------------------------------------------------------- time

def now_dhaka() -> datetime:
    return datetime.now(DHAKA)


def iso_now_dhaka() -> str:
    return now_dhaka().isoformat()


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- errors

class GitHubError(Exception):
    """Base error for classified GitHub failures."""


class NotFound(GitHubError):
    """404 / 410 (or GraphQL NOT_FOUND): the repo/ref genuinely does not exist."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Transient(GitHubError):
    """Rate limit / 429 / 5xx / timeout / network. Never a rejection."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class RateLimitExhausted(Transient):
    """Rate limit whose reset is further away than we are willing to wait."""

    def __init__(self, msg: str, reset_at: float | None = None, status: int | None = None):
        super().__init__(msg, status)
        self.reset_at = reset_at


class Unprocessable(GitHubError):
    """409 (empty repo) / 422 (bad ref) on tree fetch: treat as no SKILL.md."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


Transport = Callable[[str, str, dict[str, str], "bytes | None", float], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data)
    # URLError / TimeoutError / socket errors propagate and are classified by the client.


# --------------------------------------------------------------------------- throttle

class AdaptiveThrottle:
    """Concurrency gate whose limit halves on rate limiting and slowly recovers."""

    def __init__(self, limit: int, minimum: int = 1):
        self.max_limit = max(1, limit)
        self.limit = self.max_limit
        self.minimum = max(1, minimum)
        self.active = 0
        self.successes = 0
        self.rate_limited_events = 0
        self._cv = threading.Condition()

    @contextmanager
    def slot(self):
        with self._cv:
            while self.active >= self.limit:
                self._cv.wait(timeout=1.0)
            self.active += 1
        try:
            yield
        finally:
            with self._cv:
                self.active -= 1
                self._cv.notify_all()

    def on_rate_limit(self) -> None:
        with self._cv:
            self.rate_limited_events += 1
            self.limit = max(self.minimum, self.limit // 2)
            self.successes = 0

    def on_success(self) -> None:
        with self._cv:
            self.successes += 1
            if self.limit < self.max_limit and self.successes >= 50:
                self.limit += 1
                self.successes = 0
                self._cv.notify_all()


# --------------------------------------------------------------------------- client

class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_retries: int = 4,
        max_wait: float = 120.0,
        backoff_base: float = 1.0,
        timeout: float = 30.0,
        throttle: AdaptiveThrottle | None = None,
    ):
        self._token = token
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.clock = clock
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.throttle = throttle or AdaptiveThrottle(8)
        self.core_remaining: int | None = None
        self.core_reset: float | None = None
        self.calls: Counter = Counter()
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never leak the token
        return f"GitHubClient(token={'set' if self._token else 'none'})"

    def _rate_limit_wait(self, resp: Response) -> float | None:
        """Seconds to wait if resp is a rate-limit response, else None."""
        h = resp.headers
        text = resp.body[:500].decode("utf-8", "replace").lower() if resp.body else ""
        is_rl = resp.status == 429 or (
            resp.status == 403 and (h.get("x-ratelimit-remaining") == "0" or "rate limit" in text
                                    or "retry-after" in h)
        )
        if not is_rl:
            return None
        if h.get("retry-after"):
            try:
                return max(1.0, float(h["retry-after"]))
            except ValueError:
                pass
        if h.get("x-ratelimit-reset") and h.get("x-ratelimit-remaining") == "0":
            try:
                return max(1.0, float(h["x-ratelimit-reset"]) - self.clock() + 1.0)
            except ValueError:
                pass
        return 60.0  # secondary rate limit without hints: GitHub recommends waiting >= 60s

    def _note_rate(self, url: str, resp: Response) -> None:
        if not url.startswith(API) or url.endswith("/graphql"):
            return
        rem = resp.headers.get("x-ratelimit-remaining")
        res = resp.headers.get("x-ratelimit-reset")
        if rem is not None and resp.headers.get("x-ratelimit-resource", "core") == "core":
            with self._lock:
                try:
                    self.core_remaining = int(rem)
                    self.core_reset = float(res) if res else self.core_reset
                except ValueError:
                    pass

    def request(self, url: str, *, method: str = "GET", body: bytes | None = None,
                auth: bool = True, accept: str = "application/vnd.github+json") -> Response:
        headers = {"User-Agent": UA, "Accept": accept}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            attempt += 1
            self.calls["api" if url.startswith(API) else "raw"] += 1
            try:
                with self.throttle.slot():
                    resp = self.transport(method, url, headers, body, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as e:
                if attempt > self.max_retries:
                    raise Transient(f"network:{type(e).__name__}") from None
                self.sleep(self._backoff(attempt))
                continue
            self._note_rate(url, resp)
            if 200 <= resp.status < 300:
                self.throttle.on_success()
                return resp
            if resp.status in (404, 410):
                raise NotFound(f"HTTP {resp.status}", resp.status)
            if resp.status in (409, 422):
                raise Unprocessable(f"HTTP {resp.status}")
            wait = self._rate_limit_wait(resp)
            if wait is not None:
                self.throttle.on_rate_limit()
                last = f"rate_limited:{resp.status}"
                if wait > self.max_wait:
                    raise RateLimitExhausted(f"{last} wait={int(wait)}s", self.clock() + wait, resp.status)
                if attempt > self.max_retries:
                    raise Transient(last, resp.status)
                self.sleep(wait + random.uniform(0, 1))
                continue
            if resp.status == 403:
                # 403 without rate-limit hints (abuse detection, SSO, blocked): transient, never a rejection.
                raise Transient("HTTP 403", 403)
            if resp.status >= 500:
                if attempt > self.max_retries:
                    raise Transient(f"HTTP {resp.status}", resp.status)
                self.sleep(self._backoff(attempt))
                continue
            raise Transient(f"HTTP {resp.status}", resp.status)

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, self.backoff_base)

    def rest(self, path: str) -> Any:
        return self.request(f"{API}/{path.lstrip('/')}").json()

    def graphql(self, query: str) -> dict[str, Any]:
        body = json.dumps({"query": query}).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            payload = self.request(f"{API}/graphql", method="POST", body=body).json() or {}
            errs = payload.get("errors") or []
            if any((e or {}).get("type") == "RATE_LIMITED" for e in errs):
                self.throttle.on_rate_limit()
                if attempt > self.max_retries:
                    raise Transient("graphql RATE_LIMITED")
                self.sleep(min(self.max_wait, 60.0))
                continue
            return payload

    def raw(self, owner: str, repo: str, ref: str, path: str) -> str | None:
        url = f"{RAW}/{quote(owner)}/{quote(repo)}/{quote(ref, safe='')}/{quote(path)}"
        try:
            return self.request(url, auth=False, accept="*/*").body.decode("utf-8", "replace")
        except NotFound:
            return None


def get_gh_token() -> str | None:
    """Token from `gh auth token` (kept in memory only, never logged)."""
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                              timeout=10, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


# --------------------------------------------------------------------------- catalog

def _norm_sub(sub: str | None) -> str:
    return (sub or "").strip().strip("/").lower()


def _parent_sub(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def build_catalog_index(catalog: dict[str, Any] | Iterable[str]) -> dict[str, dict[str, Any]]:
    """repo_key -> {whole_repo, subpaths, collection}.

    Accepts a catalog dict (preferred: uses skill_path, files_found, is_collection)
    or a set of identity strings (compat with publish_lib.build_catalog_identity_set).
    """
    idx: dict[str, dict[str, Any]] = {}

    def entry(key: str) -> dict[str, Any]:
        return idx.setdefault(key.lower(), {"whole_repo": False, "subpaths": set(), "collection": False})

    if isinstance(catalog, dict):
        for s in catalog.get("skills") or []:
            raw = s.get("repo_url") or s.get("repo") or s.get("github")
            if isinstance(raw, dict):
                raw = raw.get("url") or raw.get("html_url")
            parsed = parse_github_source(str(raw) if raw else None)
            if not parsed:
                continue
            e = entry(f"{parsed['owner']}/{parsed['repo']}")
            explicit = s.get("skill_path") or s.get("subpath") or parsed.get("subpath")
            if isinstance(explicit, str) and explicit.strip():
                e["subpaths"].add(_norm_sub(explicit))
                continue
            e["whole_repo"] = True
            if s.get("is_collection"):
                e["collection"] = True
            for f in s.get("files_found") or []:
                if isinstance(f, str) and PurePosixPath(f).name.lower() == "skill.md":
                    e["subpaths"].add(_norm_sub(_parent_sub(f)))
    else:
        for ident in catalog:
            ident = str(ident).lower()
            if not ident.startswith("github:"):
                continue
            repo_key, _, sub = ident[len("github:"):].partition("::")
            e = entry(repo_key)
            if sub:
                e["subpaths"].add(_norm_sub(sub))
            else:
                e["whole_repo"] = True
    return idx


def catalog_match(idx: dict[str, dict[str, Any]], repo_key: str, subpath: str | None,
                  repo_skill_total: int, *, collection_blocks_all: bool = True) -> str | None:
    """Return a reason string if the skill is already listed, else None."""
    e = idx.get(repo_key.lower())
    if not e:
        return None
    sub = _norm_sub(subpath)
    if sub in e["subpaths"]:
        return "catalog_subpath" if sub else "catalog_root_skill"
    if e["whole_repo"]:
        if not sub:
            return "catalog_whole_repo_root_skill"
        if repo_skill_total <= 1:
            return "catalog_whole_repo_single_skill"
        if e["collection"] and collection_blocks_all:
            return "catalog_collection"
    return None


def identity_for(owner: str, repo: str, subpath: str | None) -> str:
    sub = _norm_sub(subpath)
    return f"github:{owner.lower()}/{repo.lower()}" + (f"::{sub}" if sub else "")


# --------------------------------------------------------------------------- tree / license

def _excluded(path: str) -> bool:
    return any(p.lower() in EXCLUDED_SEGMENTS for p in path.split("/")[:-1])


def _skill_sort(x: dict) -> tuple:
    sub = x.get("subpath") or ""
    return (0 if not sub else 1, sub.count("/"), sub.lower())


def discover_skills(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All SKILL.md files in a recursive tree listing: root first, then shallow -> deep."""
    dir_sha = {e["path"]: e.get("sha") for e in tree if e.get("type") == "tree"}
    found = []
    for e in tree:
        if e.get("type") != "blob":
            continue
        p = e["path"]
        if PurePosixPath(p).name.lower() != "skill.md" or _excluded(p):
            continue
        sub = _parent_sub(p)
        found.append({"skill_path": p, "subpath": sub or None, "blob_sha": e.get("sha"),
                      "folder_sha": dir_sha.get(sub) if sub else None})
    by_folder: dict[str, dict] = {}
    for f in sorted(found, key=lambda x: (PurePosixPath(x["skill_path"]).name != "SKILL.md", x["skill_path"])):
        by_folder.setdefault((f["subpath"] or "").lower(), f)
    return sorted(by_folder.values(), key=_skill_sort)


def folder_files(tree: list[dict[str, Any]], subpath: str | None, limit: int = 20) -> list[dict[str, Any]]:
    """Text files inside a skill folder for static scanning (scripts first)."""
    if not subpath:
        return []
    prefix = subpath.rstrip("/") + "/"
    files = []
    for e in tree:
        p = e.get("path", "")
        if e.get("type") != "blob" or not p.startswith(prefix):
            continue
        if not p.lower().endswith(SCAN_TEXT_EXT) or int(e.get("size") or 0) > 200_000 or _excluded(p[len(prefix):]):
            continue
        files.append({"path": p, "size": int(e.get("size") or 0)})
    files.sort(key=lambda f: (PurePosixPath(f["path"]).name.lower() != "skill.md",
                              not f["path"].lower().endswith((".sh", ".py", ".js", ".ts", ".mjs", ".ps1")),
                              f["path"]))
    return files[:limit]


def license_files(tree: list[dict[str, Any]]) -> list[str]:
    return sorted(e["path"] for e in tree if e.get("type") == "blob"
                  and LICENSE_NAME_RE.match(PurePosixPath(e["path"]).name) and not _excluded(e["path"]))


def license_tier(meta: dict[str, Any], skill_subpath: str | None, lic_files: list[str],
                 frontmatter_license: str | None) -> tuple[str, str | None, list[str]]:
    """Return (tier, license_label, evidence); tier in pass | license_review | reject."""
    info = meta.get("license") or {}
    spdx = (info.get("spdx_id") or "").strip()
    if spdx and spdx not in ("NONE", "NOASSERTION"):
        return "pass", spdx, [f"repo_spdx:{spdx}"]
    evidence: list[str] = []
    if spdx == "NOASSERTION":
        evidence.append(f"repo_license_noassertion:{info.get('name') or info.get('key') or 'other'}")
    for p in lic_files:
        if "/" not in p:
            evidence.append(f"root_license_file_unclassified:{p}")
    if skill_subpath:
        pref = skill_subpath.rstrip("/").lower() + "/"
        for p in lic_files:
            if p.lower().startswith(pref) and "/" not in p[len(pref):]:
                evidence.append(f"skill_license_file:{p}")
    if frontmatter_license:
        evidence.append(f"frontmatter_license:{frontmatter_license[:80]}")
    if evidence:
        label = frontmatter_license or (info.get("name") if spdx == "NOASSERTION" else None) or "unclassified"
        return "license_review", label, evidence
    return "reject", None, []


def redact_title(issue_num: int, title: str) -> str:
    if issue_num in CREDENTIAL_LIKE_ISSUES or CRED_TITLE_RE.search(title or ""):
        return "[REDACTED: credential-like title — publisher skip batch_seed]"
    return title


# --------------------------------------------------------------------------- state

def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class ScoutState:
    """Persistent cursor + caches under operator/state/scout (gitignored)."""

    FILES = {"cursor": "cursor.json", "repos": "repo-cache.json",
             "skills": "skill-cache.json", "scans": "scan-cache.json"}

    def __init__(self, root: Path):
        self.root = root
        self.cursor: dict[str, Any] = _load_json(root / "cursor.json", {})
        self.repos: dict[str, Any] = _load_json(root / "repo-cache.json", {})
        self.skills: dict[str, Any] = _load_json(root / "skill-cache.json", {})
        self.scans: dict[str, Any] = _load_json(root / "scan-cache.json", {})

    def save(self) -> None:
        for attr, name in self.FILES.items():
            _atomic_write(self.root / name, getattr(self, attr))

    def repo_fresh(self, key: str, pushed_at: str | None, max_age_days: int) -> dict | None:
        e = self.repos.get(key)
        if not e or not pushed_at or e.get("pushed_at") != pushed_at:
            return None
        try:
            checked = datetime.fromisoformat(e["checked_at"].replace("Z", "+00:00"))
        except Exception:
            return None
        if datetime.now(timezone.utc) - checked > timedelta(days=max_age_days):
            return None
        return e


def _scan_rules_hash() -> str:
    try:
        return hashlib.sha256((SCRIPTS / "static_scan.py").read_bytes()).hexdigest()[:12]
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- repo metadata / trees

REPO_META_FIELDS = ("nameWithOwner pushedAt isArchived isFork isEmpty stargazerCount "
                    "defaultBranchRef { name } licenseInfo { spdxId key name }")


def fetch_repo_meta(client: GitHubClient, repo_keys: list[str], batch: int = 50,
                    log: Callable[[str], None] | None = None) -> dict[str, dict[str, Any] | str]:
    """repo_key -> meta dict | 'not_found' | 'transient' (GraphQL, batched; follows renames)."""
    out: dict[str, dict[str, Any] | str] = {}
    valid = []
    for k in repo_keys:
        o, _, r = k.partition("/")
        if NAME_OK_RE.match(o or "") and NAME_OK_RE.match(r or "") and r not in (".", ".."):
            valid.append(k)
        else:
            out[k] = "not_found"
    for i in range(0, len(valid), batch):
        chunk = valid[i:i + batch]
        if log and i and (i // batch) % 10 == 0:
            log(f"  metadata {i}/{len(valid)}")
        parts = []
        for j, k in enumerate(chunk):
            o, _, r = k.partition("/")
            parts.append(f"r{j}: repository(owner: {json.dumps(o)}, name: {json.dumps(r)}) {{ {REPO_META_FIELDS} }}")
        query = "query { " + " ".join(parts) + " rateLimit { remaining resetAt cost } }"
        try:
            payload = client.graphql(query)
        except GitHubError:
            for k in chunk:
                out[k] = "transient"
            continue
        data = payload.get("data") or {}
        err_by_alias: dict[str, str] = {}
        for e in payload.get("errors") or []:
            path = (e or {}).get("path") or []
            if path:
                err_by_alias[str(path[0])] = str(e.get("type") or "ERROR")
        for j, k in enumerate(chunk):
            node = data.get(f"r{j}")
            if node:
                lic = node.get("licenseInfo") or None
                out[k] = {
                    "full_name": node.get("nameWithOwner") or k,
                    "pushed_at": node.get("pushedAt"),
                    "archived": bool(node.get("isArchived")),
                    "fork": bool(node.get("isFork")),
                    "empty": bool(node.get("isEmpty")),
                    "stars": int(node.get("stargazerCount") or 0),
                    "default_branch": (node.get("defaultBranchRef") or {}).get("name"),
                    "license": ({"spdx_id": lic.get("spdxId"), "key": lic.get("key"), "name": lic.get("name")}
                                if lic else None),
                }
            elif err_by_alias.get(f"r{j}") == "NOT_FOUND":
                out[k] = "not_found"
            else:
                out[k] = "transient"
    return out


def fetch_repo_tree_verdict(client: GitHubClient, key: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Tree-derived verdict for one repo (cacheable). Raises Transient / NotFound."""
    full = meta.get("full_name") or key
    owner, _, repo = full.partition("/")
    branch = meta.get("default_branch")
    base = {"pushed_at": meta.get("pushed_at"), "default_branch": branch,
            "checked_at": iso_now_utc(), "full_name": full}
    empty = {"status": "missing_skill", "skills": [], "skills_total": 0, "license_files": [],
             "tree_truncated": False}
    if meta.get("empty") or not branch:
        return {**base, **empty, "note": "empty_repo"}
    def get_tree(ref: str) -> dict | None:
        try:
            return client.rest(f"repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}?recursive=1") or {}
        except (Unprocessable, NotFound):
            return None

    tree = get_tree(branch)
    if tree is None:
        return {**base, **empty, "note": "tree_unprocessable"}
    entries = tree.get("tree") or []
    skills = discover_skills(entries)
    if not skills:
        # compat with the old scout's raw ref fallback (default_branch, main, master)
        for ref in [r for r in ("main", "master") if r != branch]:
            alt = get_tree(ref)
            if alt and discover_skills(alt.get("tree") or []):
                tree, entries = alt, alt.get("tree") or []
                skills = discover_skills(entries)
                base = {**base, "default_branch": ref, "skill_ref_non_default": True}
                break
    truncated = bool(tree.get("truncated"))
    if truncated:
        # partial tree (>100k entries / 7MB): also probe conventional paths via raw (no API quota)
        known = {s["skill_path"].lower() for s in skills}
        for probe in ("SKILL.md", "skill/SKILL.md", "skills/SKILL.md", ".agents/skills/SKILL.md"):
            if probe.lower() not in known and client.raw(owner, repo, branch, probe):
                sub = _parent_sub(probe)
                skills.append({"skill_path": probe, "subpath": sub or None, "blob_sha": None, "folder_sha": None})
        skills.sort(key=_skill_sort)
    kept = skills[:200]
    for s in kept:
        s["files"] = folder_files(entries, s["subpath"])
    return {**base, "status": "ok" if skills else "missing_skill", "skills": kept,
            "skills_total": len(skills), "license_files": license_files(entries)[:200],
            "tree_truncated": truncated}


# --------------------------------------------------------------------------- issues

@dataclass
class IssueTarget:
    issue: dict[str, Any]
    number: int
    owner: str
    repo: str
    subpath: str | None
    identity: str

    @property
    def repo_key(self) -> str:
        return f"{self.owner}/{self.repo}".lower()


def list_open_issues(client: GitHubClient, repo_slug: str = REPO_SLUG, max_pages: int = 60) -> list[dict]:
    """Every open issue (REST list, ~23 calls for ~2.2k issues); PRs filtered out."""
    issues: list[dict] = []
    for page in range(1, max_pages + 1):
        items = client.rest(f"repos/{repo_slug}/issues?state=open&per_page=100&page={page}"
                            f"&sort=created&direction=asc") or []
        issues.extend(it for it in items if "pull_request" not in it)
        if len(items) < 100:
            break
    return issues


def load_known_holds(art: Path = ART) -> set[int]:
    holds: set[int] = set(STATIC_HOLDS)
    for name in KNOWN_HOLDS_FILES:
        for n in _load_json(art / name, {}).get("known_holds") or []:
            try:
                holds.add(int(n))
            except (TypeError, ValueError):
                pass
    return holds


def issue_target(issue: dict[str, Any]) -> IssueTarget | None:
    for u in extract_github_urls(issue.get("title") or "", issue.get("body") or ""):
        p = parse_github_source(u)
        if p:
            sub = p.get("subpath")
            if sub and PurePosixPath(sub).name.lower() == "skill.md":
                sub = _parent_sub(sub) or None
            return IssueTarget(issue, int(issue.get("number") or 0), p["owner"], p["repo"], sub,
                               identity_for(p["owner"], p["repo"], sub))
    return None


def _covers(target_sub: str | None, skill_sub: str | None) -> bool:
    t, s = _norm_sub(target_sub), _norm_sub(skill_sub)
    return not t or s == t or s.startswith(t + "/")


# --------------------------------------------------------------------------- scout

@dataclass
class ScoutConfig:
    max_skills_per_repo: int = DEFAULTS["max_skills_per_repo"]
    max_tree_fetches: int = DEFAULTS["max_tree_fetches"]
    min_core_remaining: int = DEFAULTS["min_core_remaining"]
    workers: int = DEFAULTS["workers"]
    cache_max_age_days: int = DEFAULTS["cache_max_age_days"]
    graphql_batch: int = DEFAULTS["graphql_batch"]
    large_repo_threshold: int = DEFAULTS["large_repo_threshold"]  # 0 disables
    collection_blocks_all: bool = True
    scan: bool = True


@dataclass
class ScoutResult:
    issue_stats: Counter = field(default_factory=Counter)
    skill_stats: Counter = field(default_factory=Counter)
    pass_tier: list[dict] = field(default_factory=list)       # eligible (pre-scan)
    review_tier: list[dict] = field(default_factory=list)     # license_review (pre-scan)
    issue_outcomes: dict[int, str] = field(default_factory=dict)
    transient_repos: list[str] = field(default_factory=list)
    not_found_repos: list[str] = field(default_factory=list)
    rate_limit_exhausted: bool = False
    window: dict[str, Any] = field(default_factory=dict)
    issues_loaded: int = 0
    per_repo_new_skills: Counter = field(default_factory=Counter)
    large_collections: list[dict] = field(default_factory=list)


class Scout:
    def __init__(self, client: GitHubClient, state: ScoutState, catalog: dict[str, Any] | set[str],
                 config: ScoutConfig | None = None, known_holds: set[int] | None = None,
                 log: Callable[[str], None] = lambda m: print(m, flush=True)):
        self.client = client
        self.state = state
        self.catalog_idx = build_catalog_index(catalog)
        self.cfg = config or ScoutConfig()
        self.known_holds = set(known_holds or set()) | set(STATIC_HOLDS)
        self.log = log
        self.scan_rules = _scan_rules_hash()
        self._stop = threading.Event()

    def select_window(self, need: dict[str, int]) -> tuple[list[str], set[str], dict[str, Any]]:
        """need: repo_key -> lowest claiming issue number. Returns (ordered window, rotation keys, info).

        Order: transient repos from the last run, repos from issues opened since the last run
        (newest first), then a rotation over the rest by issue number starting after the cursor.
        """
        cur = self.state.cursor
        retry = [k for k in cur.get("retry_first", []) if k in need]
        last_max = int(cur.get("max_issue_seen") or 0)
        pos = int(cur.get("position") or 0)
        retry_set = set(retry)
        newest = sorted((k for k in need if k not in retry_set and last_max and need[k] > last_max),
                        key=lambda k: -need[k])
        taken = retry_set | set(newest)
        others = sorted((k for k in need if k not in taken), key=lambda k: (need[k], k))
        rotation = [k for k in others if need[k] > pos] + [k for k in others if need[k] <= pos]
        order = retry + newest + rotation
        window = order[: max(0, self.cfg.max_tree_fetches)]
        info = {"cursor_in": pos, "retry_first": len(retry), "new_since_last": len(newest),
                "needing_fetch": len(need), "window_size": len(window)}
        return window, set(rotation), info

    def _frontmatter(self, owner: str, repo: str, branch: str, skill: dict) -> dict[str, Any]:
        key = skill.get("blob_sha")
        if key and key in self.state.skills:
            return self.state.skills[key]
        text = self.client.raw(owner, repo, branch, skill["skill_path"])
        if text is None:
            info: dict[str, Any] = {"exists": False}
        else:
            fm = parse_skill_frontmatter(text)
            info = {"exists": True, "len": len(text), "frontmatter_license": fm.get("license") or None,
                    "name": fm.get("name") or None}
        if key:
            self.state.skills[key] = info
        return info

    def _tree_work(self, key: str, meta: dict, res: ScoutResult) -> dict | str:
        if self._stop.is_set():
            return "deferred"
        cr = self.client.core_remaining
        if cr is not None and cr < self.cfg.min_core_remaining:
            self._stop.set()
            return "deferred"
        try:
            return fetch_repo_tree_verdict(self.client, key, meta)
        except RateLimitExhausted:
            self._stop.set()
            res.rate_limit_exhausted = True
            return "transient"
        except NotFound:
            return "not_found"
        except Transient:
            return "transient"

    # -- main ------------------------------------------------------------------
    def run(self, issues: list[dict[str, Any]]) -> ScoutResult:
        res = ScoutResult(issues_loaded=len(issues))
        st = res.issue_stats
        targets: list[IssueTarget] = []
        for issue in sorted(issues, key=lambda i: int(i.get("number") or 0)):
            st["seen"] += 1
            num = int(issue.get("number") or 0)
            blocked, reason = issue_is_blocked(issue)
            if blocked:
                key = "protected" if reason == "protected_issue" else "blocked_label"
                st[key] += 1
                res.issue_outcomes[num] = key
                continue
            if num in self.known_holds:
                st["skip_known_hold"] += 1
                res.issue_outcomes[num] = "skip_known_hold"
                continue
            t = issue_target(issue)
            if not t:
                st["no_github"] += 1
                res.issue_outcomes[num] = "no_github"
                continue
            targets.append(t)

        # identical targets (same repo + same subpath): the oldest issue owns it
        seen_ident: set[str] = set()
        uniq: list[IssueTarget] = []
        for t in targets:
            if t.identity in seen_ident:
                st["dup_identity"] += 1
                res.issue_outcomes[t.number] = "dup_identity"
                continue
            seen_ident.add(t.identity)
            uniq.append(t)

        by_repo: dict[str, list[IssueTarget]] = {}
        for t in uniq:
            by_repo.setdefault(t.repo_key, []).append(t)
        repo_keys = sorted(by_repo)
        self.log(f"  issues={len(issues)} targets={len(uniq)} repos={len(repo_keys)}; metadata via GraphQL")
        metas = fetch_repo_meta(self.client, repo_keys, self.cfg.graphql_batch, self.log)

        need: dict[str, int] = {}
        verdicts: dict[str, dict | str] = {}
        for k in repo_keys:
            m = metas.get(k, "transient")
            if isinstance(m, str):
                verdicts[k] = m
                continue
            cached = self.state.repo_fresh(k, m.get("pushed_at"), self.cfg.cache_max_age_days)
            if cached:
                verdicts[k] = cached
                res.skill_stats["repo_cache_hit"] += 1
            else:
                need[k] = min(t.number for t in by_repo[k])

        window, rotation, info = self.select_window(need)
        res.window = info
        self.log(f"  cache_hits={res.skill_stats['repo_cache_hit']} need_fetch={len(need)} window={len(window)}")

        last_rot_pos: int | None = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as ex:
            results = ex.map(lambda k: (k, self._tree_work(k, metas[k], res)), window)  # type: ignore[arg-type]
            for n, (k, v) in enumerate(results, 1):
                verdicts[k] = v
                if isinstance(v, dict):
                    self.state.repos[k] = v
                if v != "deferred" and k in rotation:
                    last_rot_pos = need[k]
                if n % 100 == 0:
                    self.log(f"  tree progress {n}/{len(window)} core_remaining={self.client.core_remaining}")
        for k in need:
            verdicts.setdefault(k, "deferred")

        cur = self.state.cursor
        if all(v != "deferred" for v in verdicts.values()):
            cur["position"] = 0
            cur["last_full_cycle_at"] = iso_now_utc()
        elif last_rot_pos is not None:
            cur["position"] = last_rot_pos
        cur["retry_first"] = sorted(k for k, v in verdicts.items() if v == "transient")
        cur["max_issue_seen"] = max([int(i.get("number") or 0) for i in issues]
                                    + [int(cur.get("max_issue_seen") or 0)])
        cur["updated_at"] = iso_now_utc()
        res.window["cursor_out"] = cur.get("position", 0)
        res.window["deferred_repos"] = sum(1 for v in verdicts.values() if v == "deferred")

        self._prefetch_frontmatter(by_repo, verdicts, metas)
        for k, ts in by_repo.items():
            v = verdicts.get(k)
            if v == "not_found":
                self._mark(res, ts, "not_found")
                res.not_found_repos.append(k)
            elif v == "transient":
                self._mark(res, ts, "transient")
                res.transient_repos.append(k)
            elif not isinstance(v, dict):
                self._mark(res, ts, "deferred")
            else:
                try:
                    self._evaluate_repo(k, metas[k], v, ts, res)  # type: ignore[arg-type]
                except RateLimitExhausted:
                    res.rate_limit_exhausted = True
                    self._mark(res, ts, "transient")
                    res.transient_repos.append(k)
        cur["retry_first"] = sorted(set(cur["retry_first"]) | set(res.transient_repos))
        res.skill_stats["api_calls"] = self.client.calls["api"]
        res.skill_stats["raw_calls"] = self.client.calls["raw"]
        res.skill_stats["throttle_rate_limit_events"] = self.client.throttle.rate_limited_events
        return res

    def _prefetch_frontmatter(self, by_repo: dict[str, list[IssueTarget]], verdicts: dict, metas: dict) -> None:
        """Fetch SKILL.md text for every not-yet-cached, not-in-catalog skill in parallel (raw, no API quota)."""
        jobs = []
        for k, ts in by_repo.items():
            v = verdicts.get(k)
            if not isinstance(v, dict):
                continue
            owner, _, repo = (v.get("full_name") or k).partition("/")
            branch = v.get("default_branch") or "main"
            total = int(v.get("skills_total") or 0)
            if self._is_large(total):
                continue
            subs = [t.subpath for t in ts]
            picked = [s for s in v.get("skills") or []
                      if any(_covers(sub, s["subpath"]) for sub in subs) or any(subs)]
            for s in picked[: self.cfg.max_skills_per_repo * max(1, len(ts))]:
                if s.get("blob_sha") in self.state.skills:
                    continue
                if catalog_match(self.catalog_idx, k, s["subpath"], total,
                                 collection_blocks_all=self.cfg.collection_blocks_all):
                    continue
                jobs.append((owner, repo, branch, s))
        if not jobs:
            return
        self.log(f"  prefetch SKILL.md x{len(jobs)} (raw)")

        def one(job):
            if self._stop.is_set():
                return
            try:
                self._frontmatter(*job)
            except RateLimitExhausted:
                self._stop.set()
            except Transient:
                pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as ex:
            list(ex.map(one, jobs))

    def _is_large(self, total: int) -> bool:
        return bool(self.cfg.large_repo_threshold) and total > self.cfg.large_repo_threshold

    @staticmethod
    def _mark(res: ScoutResult, targets: list[IssueTarget], outcome: str) -> None:
        for t in targets:
            res.issue_stats[outcome] += 1
            res.issue_outcomes[t.number] = outcome

    def _evaluate_repo(self, key: str, meta: dict, verdict: dict, targets: list[IssueTarget],
                       res: ScoutResult) -> None:
        ss = res.skill_stats
        owner, _, repo = (verdict.get("full_name") or meta.get("full_name") or key).partition("/")
        branch = verdict.get("default_branch") or meta.get("default_branch") or "main"
        all_skills = verdict.get("skills") or []
        total = int(verdict.get("skills_total") or len(all_skills))
        lic_files = verdict.get("license_files") or []
        if verdict.get("tree_truncated"):
            ss["repos_tree_truncated"] += 1
        if self._is_large(total):
            # aggregator / registry monorepos: never auto-queue thousands of copied skills
            ss["skills_in_large_collections"] += total
            for t in targets:
                res.large_collections.append({"issue": t.number, "repo": key, "skills_total": total,
                                              "stars": int(meta.get("stars") or 0),
                                              "license": (meta.get("license") or {}).get("spdx_id")})
            self._mark(res, targets, "large_collection")
            return

        # a subpath that matches no SKILL.md (e.g. branch names with '/') falls back to whole repo
        eff_sub = {t.number: (t.subpath if t.subpath and any(_covers(t.subpath, s["subpath"]) for s in all_skills)
                              else None) for t in targets}
        assigned: dict[int, list[dict]] = {t.number: [] for t in targets}
        for s in all_skills:
            best: tuple[tuple[int, int], IssueTarget] | None = None
            for t in targets:
                sub = eff_sub[t.number]
                if _covers(sub, s["subpath"]):
                    rank = (len(sub or ""), -t.number)  # most specific, then oldest issue
                    if best is None or rank > best[0]:
                        best = (rank, t)
            if best:
                assigned[best[1].number].append(s)

        for t in targets:
            skills = assigned[t.number]
            ss["skills_found"] += len(skills)
            if not skills:
                self._mark(res, [t], "missing_skill")
                continue
            capped = skills[: self.cfg.max_skills_per_repo]
            ss["skills_capped_out"] += len(skills) - len(capped)
            outcomes: Counter = Counter()
            for s in capped:
                if catalog_match(self.catalog_idx, key, s["subpath"], total,
                                 collection_blocks_all=self.cfg.collection_blocks_all):
                    ss["skills_already_in_catalog"] += 1
                    outcomes["already_in_catalog"] += 1
                    continue
                try:
                    fm = self._frontmatter(owner, repo, branch, s)
                except RateLimitExhausted:
                    raise
                except Transient:
                    ss["skills_transient"] += 1
                    outcomes["transient"] += 1
                    continue
                if not fm.get("exists"):
                    ss["skills_missing_text"] += 1
                    outcomes["missing_skill"] += 1
                    continue
                tier, label, evidence = license_tier(meta, s["subpath"], lic_files, fm.get("frontmatter_license"))
                if tier == "reject":
                    ss["skills_missing_license"] += 1
                    outcomes["missing_license"] += 1
                    continue
                cand = {
                    "issue": t.number,
                    "title": redact_title(t.number, t.issue.get("title") or ""),
                    "identity": identity_for(owner, repo, s["subpath"]),
                    "owner": owner.lower(),
                    "repo": repo.lower(),
                    "repo_url": f"https://github.com/{owner.lower()}/{repo.lower()}",
                    "subpath": _norm_sub(s["subpath"]) or None,
                    "issue_subpath": t.subpath,
                    "updated_at": t.issue.get("updated_at"),
                    "created_at": t.issue.get("created_at"),
                    "stars": int(meta.get("stars") or 0),
                    "license": label,
                    "license_tier": tier,
                    "license_evidence": evidence,
                    "pushed_at": meta.get("pushed_at"),
                    "skill_path": s["skill_path"],
                    "skill_len": int(fm.get("len") or 0),
                    "default_branch": branch,
                    "repo_skills_total": total,
                    "archived": bool(meta.get("archived")),
                    # any listing of this repo already exists (publisher/website allow one listing per repo)
                    "repo_in_catalog": key.lower() in self.catalog_idx,
                    "_skill": s,
                }
                res.per_repo_new_skills[key] += 1
                if tier == "pass":
                    ss["skills_pass_tier"] += 1
                    outcomes["eligible"] += 1
                    res.pass_tier.append(cand)
                else:
                    ss["skills_license_review"] += 1
                    outcomes["license_review"] += 1
                    res.review_tier.append(cand)
            for o in ("eligible", "license_review", "already_in_catalog", "transient",
                      "missing_license", "missing_skill"):
                if outcomes[o]:
                    self._mark(res, [t], o)
                    break

    def scan(self, cand: dict[str, Any]) -> dict[str, Any]:
        return run_scan_with_client(self.client, cand, self.state, self.scan_rules)


# --------------------------------------------------------------------------- static scan

def _scan_key(cand: dict[str, Any], rules: str) -> str | None:
    s = cand.get("_skill") or {}
    if cand.get("subpath") and s.get("folder_sha"):
        return f"{rules}:tree:{s['folder_sha']}"
    if s.get("blob_sha") and cand.get("pushed_at"):
        return f"{rules}:root:{cand['owner']}/{cand['repo']}:{cand['pushed_at']}:{s['blob_sha']}"
    return None


def run_scan_with_client(client: GitHubClient, cand: dict[str, Any], state: ScoutState | None = None,
                         rules: str | None = None) -> dict[str, Any]:
    """Mandatory deterministic static scan of one skill (text only, never executed)."""
    rules = rules or _scan_rules_hash()
    key = _scan_key(cand, rules)
    base = {
        "issue": cand["issue"], "identity": cand["identity"], "stars": cand.get("stars") or 0,
        "license": cand.get("license"), "license_tier": cand.get("license_tier", "pass"),
        "owner": cand["owner"], "repo": cand["repo"], "repo_url": cand.get("repo_url"),
        "subpath": cand.get("subpath"), "skill_path": cand.get("skill_path"), "title": cand.get("title"),
    }
    if state is not None and key and key in state.scans:
        return {**base, **state.scans[key], "scan_cached": True}
    owner, repo = cand["owner"], cand["repo"]
    branch = cand.get("default_branch") or "main"
    skill_path = cand["skill_path"]
    text = client.raw(owner, repo, branch, skill_path)
    if text is None:
        return {**base, "status": "hold", "hold": "scan_error:skill_md_missing", "max_severity": "high",
                "finding_counts": {}, "ok": False, "files_scanned": 0}
    files: list[tuple[str, str]] = [("SKILL.md", text)]
    if cand.get("subpath"):
        prefix = skill_path.rsplit("/", 1)[0] + "/"
        for f in (cand.get("_skill") or {}).get("files") or []:
            if f["path"] == skill_path:
                continue
            t = client.raw(owner, repo, branch, f["path"])
            if t is not None and len(t) < 200_000:
                files.append((f["path"][len(prefix):] if f["path"].startswith(prefix) else
                              PurePosixPath(f["path"]).name, t))
    else:
        for extra in ROOT_SCAN_EXTRAS:  # same file set as the old scout for root skills
            if extra.lower() == skill_path.lower():
                continue
            t = client.raw(owner, repo, branch, extra)
            if t is not None and len(t) < 200_000:
                files.append((extra, t))
    tmp = Path(tempfile.mkdtemp(prefix=f"scout-scan-{cand['issue']}-"))
    try:
        for rel, body in files:
            rel = "/".join(p for p in rel.split("/") if p not in ("", ".", ".."))
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8", errors="replace")
        result = scan_candidate(tmp, {"max_files_per_candidate": 50,
                                      "max_total_bytes_per_candidate": 2_000_000}, project_root=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    counts: Counter = Counter(f.get("severity", "low") for f in result.get("findings") or [])
    status, hold = ("hold", "critical_static") if result.get("has_critical") else ("pass", None)
    verdict = {"status": status, "hold": hold, "max_severity": result.get("max_severity") or "none",
               "finding_counts": dict(counts), "ok": status == "pass",
               "files_scanned": result.get("files_scanned") or len(files)}
    if state is not None and key:
        state.scans[key] = verdict
    return {**base, **verdict}


def public_cand(c: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def sort_key(c: dict) -> tuple:
    return (-(int(c.get("stars") or 0)), -(int(c.get("issue") or 0)), c.get("identity") or "")


def candidate_identity(c: dict[str, Any]) -> str:
    """Skill identity (`github:owner/repo[::subpath]`, or another source prefix), lower-cased.

    Derived from owner/repo/subpath when a (legacy) entry has no explicit identity.
    """
    ident = str(c.get("identity") or "").strip()
    if ident:
        return ident.lower()
    owner, repo = c.get("owner"), c.get("repo")
    if owner and repo:
        return identity_for(str(owner), str(repo), c.get("subpath"))
    return ""


def candidate_repo_key(c: dict[str, Any]) -> str:
    owner, repo = (c.get("owner") or "").strip().lower(), (c.get("repo") or "").strip().lower()
    if owner and repo:
        return f"{owner}/{repo}"
    ident = candidate_identity(c)
    if ident.startswith("github:"):
        return ident[len("github:"):].split("::", 1)[0]
    return ident.split("::", 1)[0]


def _group_key(c: dict[str, Any]) -> str:
    issue = c.get("issue")
    try:
        n = int(issue) if issue is not None else 0
    except (TypeError, ValueError):
        n = 0
    # issue-less candidates (future non-issue sources): one rotation group per repo
    return f"issue:{n}" if n else f"repo:{candidate_repo_key(c)}"


def order_passed(passed: list[dict], max_total: int = DEFAULTS["queue_max_passed"],
                 max_per_repo: int = DEFAULTS["queue_max_per_repo"]) -> list[dict]:
    """Queue order for `passed`: round-robin across issues (then stars), at most `max_per_repo` per repo.

    * Candidates are grouped by issue (issue-less candidates by repo); inside a group by stars desc.
    * Groups are ordered by their best star count (ties: newer issue, then key).
    * Round r takes the r-th skill of every group, so one issue/monorepo cannot take all top slots.
    * Candidates whose repo already has a catalog listing (`repo_in_catalog`) come after all others:
      the publisher and the website validator currently allow one listing per GitHub repo.
    * Duplicate identities are dropped (first wins).
    """
    out: list[dict] = []
    per_repo: Counter = Counter()
    seen: set[str] = set()
    for tier in (False, True):
        groups: dict[str, list[dict]] = {}
        for p in passed:
            if bool(p.get("repo_in_catalog")) is tier:
                groups.setdefault(_group_key(p), []).append(p)
        for g in groups.values():
            g.sort(key=sort_key)
        order = sorted(groups, key=lambda k: (sort_key(groups[k][0]), k))
        depth = max((len(g) for g in groups.values()), default=0)
        for r in range(depth):
            for k in order:
                g = groups[k]
                if r >= len(g):
                    continue
                p = g[r]
                ident = candidate_identity(p)
                rk = candidate_repo_key(p)
                if ident in seen or per_repo[rk] >= max_per_repo:
                    continue
                seen.add(ident)
                per_repo[rk] += 1
                out.append(p)
                if len(out) >= max_total:
                    return out
    return out


def trim_passed(passed: list[dict], max_total: int, max_per_repo: int) -> list[dict]:
    """Compat name: the ordered, capped passed queue (see order_passed)."""
    return order_passed(passed, max_total, max_per_repo)


# --------------------------------------------------------------------------- queue helpers (publisher)

QUEUE_LIST_KEYS = ("passed", "eligible", "license_review")


def queue_path_for(day: Any = None, art: Path = ART) -> Path:
    """candidate-queue-YYYY-MM-DD.json for a Dhaka date (default today)."""
    d = day or now_dhaka().date()
    return art / f"candidate-queue-{d.isoformat() if hasattr(d, 'isoformat') else d}.json"


def merge_passed_with_eligible(queue: dict[str, Any]) -> list[dict]:
    """Full candidate records for `passed`, merged with `eligible` BY IDENTITY (never by issue number).

    Merging by issue collapses sibling skills of one issue into the same record; merging by identity keeps
    every skill distinct. Fields in `passed` win. Queue order is preserved.
    """
    elig = {candidate_identity(e): e for e in queue.get("eligible") or [] if isinstance(e, dict)}
    out = []
    for p in queue.get("passed") or []:
        if not isinstance(p, dict):
            continue
        ident = candidate_identity(p)
        out.append({**elig.get(ident, {}), **p, "identity": ident or p.get("identity")})
    return out


def remove_identities_from_queue(queue: dict[str, Any], identities: Iterable[str]) -> tuple[dict[str, Any], int]:
    """Return (new queue, removed count) without the given skill identities in passed/eligible/license_review.

    Sibling skills of the same issue survive; the issue number is never used as the key.
    """
    drop = {str(i).strip().lower() for i in identities if i and str(i).strip()}
    q = dict(queue)
    removed = 0
    for key in QUEUE_LIST_KEYS:
        items = queue.get(key)
        if not isinstance(items, list):
            continue
        kept = [x for x in items if not (isinstance(x, dict) and candidate_identity(x) in drop)]
        removed += len(items) - len(kept)
        q[key] = kept
    if "passed" in q:
        q["passed_count"] = len(q["passed"])
    if "eligible" in q:
        q["eligible_count"] = len(q["eligible"])
    return q, removed


def remove_published_from_queue(identities: Iterable[str], queue_path: Path | None = None, *,
                                run_id: str | None = None) -> dict[str, Any]:
    """Publisher helper: drop published/staged identities from the candidate queue file (atomic write).

    Returns a small summary. Missing queue file -> no-op.
    """
    path = queue_path or queue_path_for()
    ids = sorted({str(i).strip().lower() for i in identities if i and str(i).strip()})
    queue = _load_json(path, None)
    if not isinstance(queue, dict):
        return {"queue": str(path), "removed": 0, "identities": ids, "note": "queue missing"}
    newq, removed = remove_identities_from_queue(queue, ids)
    newq["last_removed_identities"] = ids
    if run_id:
        newq["last_publisher_run_id"] = run_id
    newq["last_removed_at"] = iso_now_utc()
    _atomic_write(path, newq)
    return {"queue": str(path), "removed": removed, "identities": ids,
            "passed_count": len(newq.get("passed") or [])}


COMPAT_STAT_KEYS = ("seen", "already_in_catalog", "missing_license", "missing_skill", "skip_known_hold",
                    "repo_fail", "dup_identity", "protected", "blocked_label", "no_github", "eligible",
                    "license_review", "not_found", "transient", "deferred", "large_collection")


def compat_stats(res: ScoutResult) -> dict[str, int]:
    out = {k: int(res.issue_stats.get(k, 0)) for k in COMPAT_STAT_KEYS}
    out["repo_fail"] = out["not_found"]  # compat key: only genuine 404/410 counts as a repo failure
    return out


# --------------------------------------------------------------------------- outputs

def scan_all(scout: Scout, cands: list[dict], workers: int) -> list[tuple[dict, dict]]:
    def one(c: dict) -> dict:
        try:
            return scout.scan(c)
        except Exception as e:  # a scan failure is a hold, never a pass
            return {"issue": c["issue"], "identity": c["identity"], "status": "hold",
                    "hold": f"scan_error:{type(e).__name__}", "ok": False,
                    "license_tier": c.get("license_tier")}
    out: list[tuple[dict, dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for n, pair in enumerate(zip(cands, ex.map(one, cands)), 1):
            out.append(pair)
            if n % 250 == 0:
                scout.log(f"  scan progress {n}/{len(cands)}")
    return out


def assemble(res: ScoutResult, scanned: list[tuple[dict, dict]], prev_queue: dict, *, run_id: str,
             live_count: int, queue_max_passed: int = DEFAULTS["queue_max_passed"],
             queue_max_per_repo: int = DEFAULTS["queue_max_per_repo"]) -> dict[str, Any]:
    """Build queue / eligible / batch / summary payloads (format-compatible with the /tmp scout)."""
    holds_critical, holds_other, passed, review = [], [], [], []
    for c, sr in scanned:
        pub = {**public_cand(c), "scan_status": sr.get("status"), "max_severity": sr.get("max_severity")}
        if sr.get("status") == "pass":
            (passed if c.get("license_tier") == "pass" else review).append(pub)
        else:
            h = {"issue": c["issue"], "identity": c["identity"], "hold": sr.get("hold") or "hold",
                 "license_tier": c.get("license_tier")}
            (holds_critical if sr.get("hold") == "critical_static" else holds_other).append(h)
    passed_all = sorted(passed, key=sort_key)
    passed_q = [{**p, "queue_rank": i} for i, p in
                enumerate(order_passed(passed_all, queue_max_passed, queue_max_per_repo), 1)]
    held_ids = {h["identity"] for h in holds_critical + holds_other}
    eligible_pub = sorted([public_cand(c) for c in res.pass_tier if c["identity"] not in held_ids], key=sort_key)
    prev_ids = {x.get("identity") for key in ("eligible", "passed", "license_review")
                for x in (prev_queue.get(key) or []) if isinstance(x, dict)}
    eligible_new = [c for c in eligible_pub if c["identity"] not in prev_ids]
    elig_ids = {c["identity"] for c in eligible_pub}
    warm_dropped = [{"issue": x.get("issue"), "identity": x.get("identity"), "reason": "no_longer_eligible"}
                    for x in (prev_queue.get("passed") or []) if isinstance(x, dict)
                    and x.get("identity") not in elig_ids
                    and res.issue_outcomes.get(int(x.get("issue") or 0)) not in ("deferred", "transient")]
    publisher_skip = sorted({p["issue"] for p in passed + review + eligible_pub
                             if p["issue"] in CREDENTIAL_LIKE_ISSUES or str(p.get("title") or "").startswith("[REDACTED")})
    stats = compat_stats(res)
    skill_stats = dict(res.skill_stats)
    tiers = {"pass_tier_pre_scan": len(res.pass_tier), "license_review_pre_scan": len(res.review_tier),
             "pass_scanned_clean": len(passed_all), "license_review_scanned_clean": len(review),
             "holds_critical_static": len(holds_critical), "holds_other": len(holds_other)}
    coverage = {**res.window, "rate_limit_exhausted": res.rate_limit_exhausted,
                "transient_repos": len(res.transient_repos), "not_found_repos": len(res.not_found_repos)}
    top_repos = [{"repo": k, "new_skills": n} for k, n in res.per_repo_new_skills.most_common(10)]
    curr_pass_ids = {p["identity"] for p in passed_q}
    prev_pass_ids = {p.get("identity") for p in prev_queue.get("passed") or [] if isinstance(p, dict)}
    material = bool(eligible_new or warm_dropped or prev_pass_ids != curr_pass_ids)
    common = {"generated_at": iso_now_utc(), "run_id": run_id, "materialize": MATERIALIZE,
              "scout": "operator/scripts/scout.py"}
    queue = {
        **common,
        "soft_cap": SOFT_CAP, "keep_enriching_when_budget_exhausted": True,
        "remaining_budget": prev_queue.get("remaining_budget", SOFT_CAP), "stage_budget": 0,
        "eligible_count": len(eligible_pub), "passed_count": len(passed_q),
        "source_warm": "scout.py full coverage (repo verdicts cached by pushed_at)",
        "fresh_scout_eligible_new": len(eligible_new),
        "warm_queue_eligible_retained": len(eligible_pub) - len(eligible_new),
        "warm_queue_passed_retained": len(curr_pass_ids & prev_pass_ids),
        "eligible": eligible_pub,
        # `passed` carries full candidate fields and is already in publish order (round-robin by issue,
        # then stars; <= queue_max_per_repo per repo). Key entries by `identity`, never by issue number:
        # use merge_passed_with_eligible() / remove_published_from_queue().
        "passed": passed_q,
        "passed_order": "round_robin_by_issue_then_stars; repo_in_catalog last; max_per_repo=%d" % queue_max_per_repo,
        "passed_total_before_trim": len(passed_all),
        "license_review": sorted(review, key=sort_key),
        "license_review_note": "NOT publishable by default; the publisher reads only `passed` (pass tier).",
        "batch_seed": [], "batch_size": 0,
        "publisher_skip_batch_seed": publisher_skip,
        "note": f"scout.py enrichment-only; stage_budget=0; eligible_new={len(eligible_new)}",
        "stats": stats, "skill_stats": skill_stats, "tiers": tiers,
        "verified_today": prev_queue.get("verified_today", 0),
        "holds_critical_static": holds_critical, "holds_other_scan": holds_other,
        "warm_dropped": warm_dropped,
        "last_publisher_run_id": prev_queue.get("last_publisher_run_id"),
        "last_intake_run_id": run_id,
        "coverage": coverage, "top_repos_by_new_skills": top_repos,
        "large_collections": sorted(res.large_collections, key=lambda x: -x["skills_total"]),
        "large_collections_note": "repos above large_repo_threshold SKILL.md files; not auto-queued, human review",
    }
    eligible_art = {**common, "scout_passes": ["ALL_OPEN_ISSUES"], "stats": stats, "skill_stats": skill_stats,
                    "tiers": tiers, "issues_loaded": res.issues_loaded, "eligible": eligible_pub,
                    "eligible_new": eligible_new, "license_review": [public_cand(c) for c in res.review_tier],
                    "coverage": coverage}
    batch = {**common, "dry_run": True, "total": 0, "candidates": [],
             "stats": {"issues_loaded": res.issues_loaded, "eligible": len(eligible_pub),
                       "scanned": len(scanned), "passed": len(passed_q), "selected": 0, "stage_budget": 0,
                       "scout_stats": stats},
             "holds_critical_static": holds_critical, "holds_other_scan": holds_other,
             "live_catalog_count": live_count, "publisher_skip_batch_seed": publisher_skip}
    scout_block = {"passes": ["ALL_OPEN_ISSUES"], "seen": stats["seen"], "eligible": len(eligible_pub),
                   "eligible_new": len(eligible_new), "scanned": len(scanned), "passed": len(passed_q),
                   "selected": 0, "stats": stats, "skill_stats": skill_stats, "tiers": tiers,
                   "issues_loaded": res.issues_loaded, "materialize": MATERIALIZE, "executed_by": run_id,
                   "publisher_skip_batch_seed": publisher_skip, "coverage": coverage,
                   "top_repos_by_new_skills": top_repos}
    return {"queue": queue, "eligible": eligible_art, "batch": batch, "scout": scout_block,
            "material_change": material, "scan_results": [sr for _, sr in scanned],
            "passed_issues": sorted({p["issue"] for p in passed_q if p.get("issue")}),
            "passed_identities": [p["identity"] for p in passed_q], "warm_dropped": warm_dropped}


# --------------------------------------------------------------------------- CLI

def acquire_intake_lock(run_id: str) -> Path | None:
    """Same semantics as the /tmp scout: skip if any live lock is held; clear dead ones."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    for p in LOCKS.glob("*.lock"):
        try:
            raw = p.read_text(encoding="utf-8").strip()
            data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw) if raw.isdigit() else None}
        except Exception:
            data = {}
        pid = data.get("pid")
        if pid and Path(f"/proc/{pid}").exists():
            return None
        try:
            p.unlink()
        except Exception:
            pass
    path = LOCKS / "intake-enrich.lock"
    path.write_text(json.dumps({"run_id": run_id, "pid": os.getpid(), "acquired_at_asia_dhaka": iso_now_dhaka(),
                                "routine": "intake_enrichment"}, indent=2) + "\n", encoding="utf-8")
    atexit.register(lambda: path.unlink(missing_ok=True))
    return path


@contextmanager
def scout_state_lock(root: Path):
    """Private lock for the scout cache (kept OUT of state/locks so routines never LOCK_SKIP on it)."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / "scout.lock"
    if p.exists():
        try:
            pid = int(json.loads(p.read_text()).get("pid") or 0)
        except Exception:
            pid = 0
        if pid and Path(f"/proc/{pid}").exists():
            raise SystemExit(f"scout state busy (pid {pid})")
    p.write_text(json.dumps({"pid": os.getpid(), "at": iso_now_dhaka()}))
    try:
        yield
    finally:
        p.unlink(missing_ok=True)


def load_catalog_live(snapshot: Path | None) -> dict[str, Any]:
    if snapshot:
        return json.loads(snapshot.read_text(encoding="utf-8"))
    req = urllib.request.Request(LIVE_CATALOG_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ProSkills committed intake scout")
    ap.add_argument("--write", action="store_true",
                    help="routine mode: write candidate-queue-YYYY-MM-DD.json + intake-* artifacts. "
                         "Default is dry-run (scout-dryrun-* artifacts + scout cache only).")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--catalog", type=Path, default=None, help="catalog snapshot (default: live fetch)")
    ap.add_argument("--state-dir", type=Path, default=SCOUT_STATE)
    ap.add_argument("--no-persist-state", action="store_true")
    ap.add_argument("--max-tree-fetches", type=int, default=DEFAULTS["max_tree_fetches"])
    ap.add_argument("--min-core-remaining", type=int, default=DEFAULTS["min_core_remaining"])
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    ap.add_argument("--max-skills-per-repo", type=int, default=DEFAULTS["max_skills_per_repo"])
    ap.add_argument("--large-repo-threshold", type=int, default=DEFAULTS["large_repo_threshold"],
                    help="repos with more SKILL.md files go to large_collection review (0 = off)")
    ap.add_argument("--queue-max-passed", type=int, default=DEFAULTS["queue_max_passed"])
    ap.add_argument("--queue-max-per-repo", type=int, default=DEFAULTS["queue_max_per_repo"])
    ap.add_argument("--remove-identity", action="append", default=None, metavar="IDENTITY",
                    help="queue maintenance only (no scouting): remove this skill identity from the candidate "
                         "queue's passed/eligible/license_review lists. Repeatable.")
    ap.add_argument("--queue", type=Path, default=None, help="queue file for --remove-identity (default: today's)")
    args = ap.parse_args(argv)
    if args.remove_identity:
        print(json.dumps(remove_published_from_queue(args.remove_identity, args.queue, run_id=args.run_id)), flush=True)
        return 0
    write = bool(args.write)
    stamp = now_dhaka().strftime("%Y-%m-%d-%H%M")
    run_id = args.run_id or (f"intake-{stamp}" if write else f"scout-dryrun-{stamp}")
    ART.mkdir(parents=True, exist_ok=True)
    if write and acquire_intake_lock(run_id) is None:
        _atomic_write(ART / f"{run_id}-parent-result.json", {
            "run_id": run_id, "lock_skip": True, "needs_user_message": False, "material_change": False,
            "consecutive_failure": False, "completed_at": iso_now_dhaka()})
        print("LOCK_SKIP", flush=True)
        return 0
    with scout_state_lock(args.state_dir):
        return _run(args, write, run_id)


def _run(args: argparse.Namespace, write: bool, run_id: str) -> int:
    t0 = time.time()
    print(f"[{iso_now_dhaka()}] start {run_id} mode={'write' if write else 'dry-run'}", flush=True)
    client = GitHubClient(get_gh_token(), throttle=AdaptiveThrottle(args.workers))
    catalog = load_catalog_live(args.catalog)
    live_count = len(catalog.get("skills") or [])
    state = ScoutState(args.state_dir)
    cfg = ScoutConfig(max_skills_per_repo=args.max_skills_per_repo, max_tree_fetches=args.max_tree_fetches,
                      min_core_remaining=args.min_core_remaining, workers=args.workers,
                      large_repo_threshold=args.large_repo_threshold)
    today = now_dhaka().date()
    queue_path = ART / f"candidate-queue-{today.isoformat()}.json"
    prev_queue = _load_json(queue_path, {}) or _load_json(
        ART / f"candidate-queue-{(today - timedelta(days=1)).isoformat()}.json", {})
    holds = load_known_holds()
    for h in prev_queue.get("holds_critical_static") or []:
        try:
            holds.add(int(h["issue"]))
        except Exception:
            pass
    scout = Scout(client, state, catalog, cfg, holds)
    try:
        issues = list_open_issues(client)
    except GitHubError as e:
        print(f"ABORT: could not list issues ({e}); nothing written", flush=True)
        return 2
    print(f"  open issues={len(issues)} catalog={live_count} known_holds={len(holds)}", flush=True)
    res = scout.run(issues)
    if not args.no_persist_state:
        state.save()  # keep tree work even if the scan phase is interrupted
    cands = res.pass_tier + res.review_tier
    print(f"  static_scan {len(cands)} skills (pass={len(res.pass_tier)} review={len(res.review_tier)})", flush=True)
    scanned = scan_all(scout, cands, args.workers)
    state.cursor["last_run_id"] = run_id
    if not args.no_persist_state:
        state.save()
    out = assemble(res, scanned, prev_queue, run_id=run_id, live_count=live_count,
                   queue_max_passed=args.queue_max_passed, queue_max_per_repo=args.queue_max_per_repo)
    elapsed = round(time.time() - t0, 1)
    summary = {
        "run_id": run_id, "routine": "ProSkills submission intake", "completed_at": iso_now_dhaka(),
        "timezone": "Asia/Dhaka", "action": "scout_enrich_only_no_stage" if write else "scout_dry_run",
        "lock_skip": False, "live_catalog_count": live_count,
        "live_catalog_generated_at": catalog.get("generated_at"), "live_catalog_url": LIVE_CATALOG_URL,
        "stage_budget": 0, "enrichment_ran": True, "fresh_scout": True, "scout": out["scout"],
        "holds_this_run": {"critical_static": out["queue"]["holds_critical_static"],
                           "other": out["queue"]["holds_other_scan"]},
        "ai_used": 0, "selected": 0, "staged_this_run": 0, "auto_merge_performed": False,
        "stuck_pr_check": "not performed by scout.py (scout-only; no PR/merge actions)",
        "pr_29_untouched": True, "protected_holds_untouched": True,
        "material_change": out["material_change"], "needs_user_message": False, "user_facing_text": None,
        "consecutive_failure": False, "elapsed_s": elapsed, "failures": [],
    }
    if write:
        out["batch"]["dry_run"] = False
        _atomic_write(queue_path, out["queue"])
        _atomic_write(ART / f"{run_id}-eligible.json", out["eligible"])
        _atomic_write(ART / f"{run_id}-batch-candidates.json", out["batch"])
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / f"intake-{today.isoformat()}.md").open("a", encoding="utf-8") as f:
            f.write(f"\n\n## {run_id} (scout.py, completed {now_dhaka():%Y-%m-%d %H:%M} Asia/Dhaka)\n\n"
                    f"- **stats:** {json.dumps(out['scout']['stats'])}\n"
                    f"- **tiers:** {json.dumps(out['scout']['tiers'])}\n"
                    f"- **coverage:** {json.dumps(out['scout']['coverage'])}\n"
                    f"- no staging / merge / messaging; AI=0\n")
    else:
        _atomic_write(ART / f"{run_id}-queue-preview.json", out["queue"])
    _atomic_write(ART / f"{run_id}-summary.json", summary)
    _atomic_write(ART / f"{run_id}-scan-results.json", out["scan_results"])
    result = {"run_id": run_id, "completed_at": iso_now_dhaka(), "lock_skip": False, "dry_run": not write,
              "live_catalog_count": live_count, "scout": out["scout"],
              "holds_critical_static": out["scout"]["tiers"]["holds_critical_static"],
              "holds_other": out["scout"]["tiers"]["holds_other"], "ai_used": 0,
              "auto_merge_performed": False, "pr_29_untouched": True,
              "material_change": out["material_change"], "needs_user_message": False,
              "consecutive_failure": False, "passed_issues": out["passed_issues"],
              "warm_dropped_count": len(out["warm_dropped"])}
    _atomic_write(ART / f"{run_id}-parent-result.json", result)
    print(json.dumps({k: out["scout"][k] for k in ("stats", "tiers", "skill_stats", "coverage",
                                                   "eligible_new", "top_repos_by_new_skills")}
                     | {"elapsed_s": elapsed}, indent=2), flush=True)
    return 0


# --------------------------------------------------------------------------- compat shims
# The hourly publisher loads the intake module via importlib and calls these names, so it can
# point at this file instead of a /tmp script.

def _compat_client(token: str | None) -> GitHubClient:
    return GitHubClient(token, throttle=AdaptiveThrottle(8))


def license_ok(license_obj: dict | None) -> tuple[bool, str | None]:
    tier, label, _ = license_tier({"license": license_obj}, None, [], None)
    return tier == "pass", label if tier == "pass" else (license_obj or {}).get("spdx_id")


def fetch_issues_pass(order_by_field: str, direction: str, want: int, skip: set[int]) -> list[dict]:
    """Compat with the /tmp scout signature; lists open issues via REST (no 1000-result search cap)."""
    token = get_gh_token()
    issues = list_open_issues(_compat_client(token))
    key = "created_at" if order_by_field == "CREATED_AT" else "updated_at"
    issues.sort(key=lambda i: i.get(key) or "", reverse=(direction == "DESC"))
    out = []
    for it in issues:
        n = int(it.get("number") or 0)
        if n in skip:
            continue
        skip.add(n)
        out.append(it)
        if len(out) >= want:
            break
    return out


def run_scan(cand: dict, token: str | None) -> dict:
    return run_scan_with_client(_compat_client(token), cand)


def rematerialize_warm(c: dict, token: str | None, catalog_ids: set[str] | dict) -> dict | None:
    """Re-verify one queued candidate (pass tier only). Returns refreshed cand or None."""
    num = int(c.get("issue") or 0)
    if num in PROTECTED_ISSUES or num in STATIC_HOLDS:
        return None
    client = _compat_client(token)
    key = f"{c['owner']}/{c['repo']}".lower()
    meta = fetch_repo_meta(client, [key]).get(key)
    if not isinstance(meta, dict):
        return None
    try:
        verdict = fetch_repo_tree_verdict(client, key, meta)
    except GitHubError:
        return None
    sub = _norm_sub(c.get("subpath"))
    skill = next((s for s in verdict.get("skills") or [] if _norm_sub(s["subpath"]) == sub), None)
    if not skill or catalog_match(build_catalog_index(catalog_ids), key, skill["subpath"],
                                  int(verdict.get("skills_total") or 0)):
        return None
    tier, label, evidence = license_tier(meta, skill["subpath"], verdict.get("license_files") or [], None)
    if tier != "pass":
        return None
    return {**public_cand(c), "license": label, "license_tier": tier, "license_evidence": evidence,
            "stars": meta.get("stars") or 0, "pushed_at": meta.get("pushed_at"),
            "skill_path": skill["skill_path"], "default_branch": verdict.get("default_branch") or "main",
            "title": redact_title(num, c.get("title") or ""), "_skill": skill}


def classify_batch(issues: list[dict], catalog_ids: set[str] | dict, known_holds: set[int],
                   token: str | None, workers: int = 8) -> tuple[Counter, list[dict], set[str]]:
    """Compat: classify issues without persisting state. Returns (stats, pass-tier eligible, identities)."""
    with tempfile.TemporaryDirectory() as tmp:
        scout = Scout(_compat_client(token), ScoutState(Path(tmp)), catalog_ids,
                      ScoutConfig(workers=workers, max_tree_fetches=10_000), known_holds, log=lambda m: None)
        res = scout.run(issues)
    return Counter(compat_stats(res)), res.pass_tier, {c["identity"] for c in res.pass_tier + res.review_tier}


if __name__ == "__main__":
    sys.exit(main())
