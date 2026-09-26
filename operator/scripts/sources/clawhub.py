"""Adapter: ClawHub public skills feed (https://clawhub.ai/v1/feeds/skills).

robots.txt on clawhub.ai: `Disallow: /api/`, `Allow: /v1/feeds/skills`. This adapter therefore
reads only the feed and the public skill pages (`/<owner>/skills/<slug>`); it never calls
`/api/...` (checked at runtime against robots.txt). Low rate (>= 2 s between page fetches),
page extracts cached per skill version.

* Skill backed by GitHub (page field `githubSourceRepo` + `githubPath`, or a `repository:` GitHub URL in
  the SKILL.md frontmatter) -> a GitHub observation (source `clawhub`, ClawHub metrics). It then goes
  through the normal GitHub checks.
* ClawHub-only skill -> record with `source_type: clawhub`, identity `clawhub:@owner/slug`,
  license MIT-0 (platform-level; evidence: ClawHub skill-format docs), SKILL.md text from the public
  page for the mandatory static_scan.
"""
from __future__ import annotations

import html
import math
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import DiskCache, iso, observation

NAME = "clawhub"
BASE = "https://clawhub.ai"
FEED_URL = f"{BASE}/v1/feeds/skills"
ROBOTS_URL = f"{BASE}/robots.txt"
LICENSE_EVIDENCE_URL = "https://github.com/openclaw/clawhub/blob/HEAD/docs/skill-format.md"
LICENSE_EVIDENCE_TEXT = "All skills published on ClawHub are licensed under MIT-0 (ClawHub skill-format docs)"
UA = "proskills-operator-sources/1.0 (+https://proskills.md)"
SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".rb", ".go", ".rs", ".pl",
              ".php", ".exe", ".bin", ".bat", ".cmd")
ID_RE = re.compile(r"^@([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,200})$")

Fetch = Callable[[str], "tuple[int, str]"]


def default_fetch(url: str, timeout: float = 30.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


# --------------------------------------------------------------------------- robots

def robots_rules(text: str) -> list[tuple[str, str]]:
    """[(allow|disallow, path)] for `User-agent: *` groups."""
    rules, active = [], False
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            active = v == "*"
        elif active and k in ("allow", "disallow") and v:
            rules.append((k, v))
    return rules


def robots_allows(rules: list[tuple[str, str]], path: str) -> bool:
    """Longest-match rule wins (Google semantics); allow on tie; default allow."""
    best: tuple[int, bool] | None = None
    for kind, prefix in rules:
        if path.startswith(prefix):
            cand = (len(prefix), kind == "allow")
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1]):
                best = cand
    return True if best is None else best[1]


# --------------------------------------------------------------------------- page parsing

def page_url(owner: str, slug: str) -> str:
    return f"{BASE}/{owner}/skills/{slug}"


def html_to_text(fragment: str) -> str:
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|li|h[1-6]|pre|tr|div|blockquote|figure|code)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _js_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern + r':"((?:[^"\\]|\\.)*)"', text)
    return m.group(1) if m else None


def parse_page(text: str) -> dict[str, Any]:
    """Extract what we need from a public skill page (SSR HTML + serialized payload)."""
    out: dict[str, Any] = {"ok": bool(text)}
    start = text.find('id="skill-tabpanel-readme"')
    if start >= 0:
        gt = text.find(">", start)
        end = text.find('id="skill-tabpanel-', start + 30)
        frag = text[gt + 1: end if end > 0 else gt + 400_000]
        out["skill_md_text"] = html_to_text(frag)[:200_000]
    else:
        out["skill_md_text"] = None
    out["github_source_repo"] = _js_str("githubSourceRepo", text)
    out["github_path"] = _js_str("githubPath", text)
    out["github_commit"] = _js_str("githubCurrentCommit", text)
    m = re.search(r"repository:\s*(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    out["frontmatter_repository"] = m.group(1).rstrip(".") if m else None
    out["page_license"] = _js_str("license", text)
    m = re.search(r"stats:\$R\[\d+\]=\{([^}]*)\}", text)
    stats: dict[str, int] = {}
    if m:
        for k, v in re.findall(r"(\w+):(\d+)", m.group(1)):
            stats[k] = int(v)
    out["stats"] = stats
    out["files"] = sorted(set(re.findall(r'\$R\[\d+\]=\{contentType:"[^"]*",path:"([^"]+)"', text)))[:500]
    out["clawhub_critical_findings"] = len(re.findall(r'severity:"critical"', text))
    out["parsed_description"] = _js_str("description", text)
    out["moderation"] = moderation_flags(text)
    return out


def moderation_flags(text: str) -> dict[str, Any]:
    """ClawHub platform moderation / scanner signals from the serialized page payload."""
    verdicts = sorted(set(re.findall(r'verdict:"([a-z_]+)"', text)) | set(re.findall(r'status:"(suspicious|malicious)"', text)))
    flags = {"is_suspicious": "isSuspicious:!0" in text, "malware_blocked": "isMalwareBlocked:!0" in text,
             "hidden_by_mod": "isHiddenByMod:!0" in text, "scanner_verdicts": verdicts}
    flags["flagged"] = bool(flags["is_suspicious"] or flags["malware_blocked"] or flags["hidden_by_mod"]
                            or any(v in ("suspicious", "malicious") for v in verdicts))
    return flags


# --------------------------------------------------------------------------- client

class ClawHubClient:
    def __init__(self, cache: DiskCache, *, fetch: Fetch = default_fetch, min_interval: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 feed_ttl_s: float = 6 * 3600, page_ttl_s: float = 7 * 86400):
        self.cache = cache
        self.fetch = fetch
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self.feed_ttl_s = feed_ttl_s
        self.page_ttl_s = page_ttl_s
        self._last: float | None = None
        self.requests = 0
        self.rules: list[tuple[str, str]] | None = None

    def _get(self, url: str) -> tuple[int, str]:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "/" in url.split("://", 1)[-1] else "/"
        if self.rules is not None and not robots_allows(self.rules, path):
            raise PermissionError(f"robots.txt disallows {path}")
        if path.startswith("/api/"):
            raise PermissionError("never call /api/ (robots)")
        if self._last is not None:
            d = self.min_interval - (self.clock() - self._last)
            if d > 0:
                self.sleep(d)
        self._last = self.clock()
        self.requests += 1
        return self.fetch(url)

    def load_robots(self) -> list[tuple[str, str]]:
        cached = self.cache.get("clawhub:robots", 86400)
        if cached is None:
            status, text = self._get(ROBOTS_URL)
            cached = text if status == 200 else ""
            self.cache.put("clawhub:robots", cached)
        self.rules = robots_rules(cached)
        return self.rules

    def feed(self) -> dict[str, Any] | None:
        import json
        cached = self.cache.get("clawhub:feed", self.feed_ttl_s)
        if cached is not None:
            return cached
        if not robots_allows(self.rules or [], "/v1/feeds/skills"):
            return None
        status, text = self._get(FEED_URL)
        if status != 200:
            return None
        data = json.loads(text)
        self.cache.put("clawhub:feed", data)
        return data

    def page(self, owner: str, slug: str, version: str | None, *, allow_fetch: bool = True) -> dict[str, Any] | None:
        key = f"clawhub:page:@{owner}/{slug}@{version or '?'}".lower()
        cached = self.cache.get(key, self.page_ttl_s)
        if cached is not None or not allow_fetch:
            return cached
        status, text = self._get(page_url(owner, slug))
        if status != 200:
            return {"ok": False, "status": status}
        parsed = parse_page(text)
        self.cache.put(key, parsed)
        return parsed


# --------------------------------------------------------------------------- catalog / records

def clawhub_catalog_index(catalog: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(owner/slug keys, bare slugs) of ClawHub listings already in the catalog."""
    full, slugs = set(), set()
    for s in catalog.get("skills") or []:
        for u in (s.get("repo_url"), (s.get("external_ratings") or {}).get("clawhub_url")):
            if not u or "clawhub.ai" not in str(u):
                continue
            parts = [p for p in str(u).split("clawhub.ai", 1)[1].split("?")[0].split("/") if p]
            if not parts:
                continue
            slug = parts[-1].lower()
            slugs.add(slug)
            if len(parts) >= 3 and parts[1] == "skills":
                full.add(f"{parts[0].lstrip('@').lower()}/{slug}")
            elif len(parts) == 2 and parts[0].startswith("@"):
                full.add(f"{parts[0][1:].lower()}/{slug}")
    return full, slugs


def clawhub_identity(owner: str, slug: str) -> str:
    return f"clawhub:@{owner.lower()}/{slug.lower()}"


def github_mapping(page: dict[str, Any]) -> tuple[str, str, str | None, str] | None:
    """(owner, repo, subpath, how) when the ClawHub skill maps to a GitHub repo."""
    src = page.get("github_source_repo")
    if src and "/" in src:
        o, _, r = src.partition("/")
        return o, r, (page.get("github_path") or "").strip("/") or None, "githubSourceRepo"
    fr = page.get("frontmatter_repository")
    if fr:
        parts = fr.split("github.com/", 1)[1].split("/")
        if len(parts) >= 2:
            return parts[0], parts[1].removesuffix(".git"), None, "frontmatter_repository"
    return None


def scan_skill_md(text: str) -> dict[str, Any]:
    """Mandatory static scan of the published SKILL.md text (never executed)."""
    from static_scan import scan_candidate
    tmp = Path(tempfile.mkdtemp(prefix="clawhub-scan-"))
    try:
        (tmp / "SKILL.md").write_text(text, encoding="utf-8")
        res = scan_candidate(tmp, {"max_files_per_candidate": 5, "max_total_bytes_per_candidate": 1_000_000},
                             project_root=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    counts: dict[str, int] = {}
    for f in res.get("findings") or []:
        counts[f.get("severity", "low")] = counts.get(f.get("severity", "low"), 0) + 1
    return {"critical": bool(res.get("has_critical")), "max_severity": res.get("max_severity") or "none",
            "finding_counts": counts, "files_scanned": res.get("files_scanned") or 1}


def score(stats: dict[str, int], featured: bool) -> float:
    return round(math.log10(int(stats.get("downloads") or 0) + 1) + 0.5 * math.log10(int(stats.get("installs") or 0) + 1)
                 + 0.3 * math.log10(int(stats.get("stars") or 0) + 1) + (0.3 if featured else 0.0), 4)


def collect(client: ClawHubClient, catalog: dict[str, Any], *, max_pages: int = 150,
            scan: bool = True, now_iso: str | None = None) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Returns (github_observations, clawhub_only_records, info)."""
    info: dict[str, Any] = {"robots_allows_feed": None, "api_allowed": None, "feed_entries": 0,
                            "pages_fetched": 0, "pages_cached": 0, "pages_deferred": 0}
    rules = client.load_robots()
    info["robots_allows_feed"] = robots_allows(rules, "/v1/feeds/skills")
    info["api_allowed"] = robots_allows(rules, "/api/v1/skills")
    feed = client.feed()
    if not feed:
        info["error"] = "feed_unavailable_or_disallowed"
        return [], [], info
    entries = [e for e in feed.get("entries") or [] if e.get("type") == "skill" and e.get("state") == "available"]
    info["feed_entries"] = len(entries)
    info["feed_generated_at"] = feed.get("generatedAt")
    full_idx, slug_idx = clawhub_catalog_index(catalog)
    obs, records = [], []
    fetched = 0
    now_iso = now_iso or iso()
    for e in entries:
        m = ID_RE.match(str(e.get("id") or ""))
        if not m:
            continue
        owner, slug = m.group(1), m.group(2)
        url = page_url(owner, slug)
        ident = clawhub_identity(owner, slug)
        base_metrics = {"version": e.get("version"), "publisher_trust": (e.get("publisher") or {}).get("trust"),
                        "featured": bool(e.get("featured"))}
        # already listed as a ClawHub skill? (no page fetch needed)
        dup = f"{owner.lower()}/{slug.lower()}" in full_idx or slug.lower() in slug_idx
        page = client.page(owner, slug, e.get("version"), allow_fetch=False)
        if page is None and not dup:
            if fetched >= max_pages:
                info["pages_deferred"] += 1
                continue
            page = client.page(owner, slug, e.get("version"))
            fetched += 1
            info["pages_fetched"] += 1
        elif page is not None:
            info["pages_cached"] += 1
        page = page or {}
        stats = page.get("stats") or {}
        metrics = {**base_metrics, **{f"clawhub_{k}": v for k, v in stats.items()}}
        gh = github_mapping(page) if page.get("ok") else None
        if gh:
            o, r, sub, how = gh
            obs.append(observation(o, r, NAME, url, subpath=sub, observed_at=now_iso,
                                   metrics={**metrics, "mapping": how, "clawhub_id": e.get("id"),
                                            "github_commit": page.get("github_commit")}))
            continue
        rec: dict[str, Any] = {
            "identity": ident, "source_type": "clawhub", "issue": None, "clawhub_id": e.get("id"),
            "owner": owner.lower(), "slug": slug.lower(), "repo_url": url, "source_url": url,
            "commit_sha": None, "version": e.get("version"),
            "integrity": ((e.get("install") or {}).get("candidates") or [{}])[0].get("integrity"),
            "license_spdx": "MIT-0", "license": "MIT-0", "license_tier": "pass",
            "license_evidence": [f"clawhub_platform_license:MIT-0", f"evidence_url:{LICENSE_EVIDENCE_URL}",
                                 LICENSE_EVIDENCE_TEXT],
            "skill_path": "SKILL.md", "subpath": None, "stars": None, "forks": None,
            "created_at": None, "pushed_at": None, "skill_name": e.get("title"),
            "skill_description": (e.get("description") or "")[:300] or None,
            "sources": [{"source": NAME, "source_url": url, "observed_at": now_iso, "metrics": metrics}],
            "existing_issue": None, "repo_has_open_issue": False,
            "lane": "clawhub", "score": score(stats, bool(e.get("featured"))), "multi_source_count": 1,
            "bundle_files": page.get("files") or [],
        }
        if dup:
            rec.update({"status": "already_in_catalog", "catalog_reason": "catalog_clawhub_slug"})
            records.append(rec)
            continue
        if not page.get("ok"):
            rec.update({"status": "transient", "hold": f"page_status:{page.get('status')}"})
            records.append(rec)
            continue
        pl = page.get("page_license")
        if pl:
            rec["license_evidence"].append(f"page_license:{pl}")
            if pl.upper() != "MIT-0":
                rec.update({"license_tier": "license_review", "license": pl})
        text = page.get("skill_md_text")
        if not text:
            rec.update({"status": "hold", "hold": "scan_error:skill_md_missing"})
            records.append(rec)
            continue
        if not scan:
            rec["status"] = "scan_deferred"
            records.append(rec)
            continue
        sr = scan_skill_md(text)
        scripts = [f for f in rec["bundle_files"] if f.lower().endswith(SCRIPT_EXT)]
        rec["scan"] = {**sr, "coverage": "skill_md_text_from_public_page", "unscanned_scripts": scripts[:20],
                       "clawhub_critical_findings": page.get("clawhub_critical_findings", 0)}
        if sr["critical"]:
            rec.update({"status": "hold", "hold": "critical_static"})
        elif scripts:
            # bundle scripts are only reachable through /api/ (robots-disallowed): cannot be scanned -> hold
            rec.update({"status": "hold", "hold": "unscanned_bundle_scripts"})
        elif page.get("clawhub_critical_findings"):
            rec.update({"status": "hold", "hold": "clawhub_static_critical"})
        elif (page.get("moderation") or {}).get("flagged"):
            rec.update({"status": "hold", "hold": "clawhub_moderation_flag"})
        else:
            rec["status"] = "pass" if rec["license_tier"] == "pass" else "license_review"
        records.append(rec)
    info["github_mapped"] = len(obs)
    info["clawhub_only_records"] = len(records)
    info["requests"] = client.requests
    return obs, records, info
