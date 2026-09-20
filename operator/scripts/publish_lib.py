"""Shared helpers for operator publish path (stdlib only; no AI).

Identity keys use github:owner/repo or github:owner/repo::subpath.
Catalog dedupe matches normalized repo_url + optional skill_path/subpath.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

GITHUB_HOSTS = {"github.com", "www.github.com"}
LIVE_CATALOG_URL = "https://proskills.md/skills-catalog.json"
PROTECTED_ISSUES = frozenset({714, 3644, 4353, 5214, 5226, 5403, 2028, 2029, 2030, 2850})
BLOCKED_LABELS = frozenset({"blocked:no-github-repo", "curio:duplicate", "groot:published"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_GH_SSH = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")
_GH_SHORT = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?$")
_TREE_BLOB = {"tree", "blob"}
_URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.I)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, fallback: str = "skill") -> str:
    s = (text or "").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s or s == "undefined":
        s = fallback
    return s[:80].rstrip("-") or fallback


def _clean_subpath(parts: list[str]) -> str | None:
    if not parts:
        return None
    cleaned = "/".join(p for p in parts if p).strip("/")
    return cleaned.lower() if cleaned else None


def parse_github_source(raw: str | None) -> dict[str, Any] | None:
    """Parse URL into owner/repo + optional subpath.

    Returns dict: owner, repo, repo_url, subpath, identity (github:owner/repo[::subpath])
    """
    if raw is None:
        return None
    u = str(raw).strip()
    if not u or u.lower() in {"none", "null", "undefined", "n/a"}:
        return None

    m = _GH_SSH.match(u)
    if m:
        owner, repo = m.group(1).lower(), m.group(2).removesuffix(".git").lower()
        return _identity(owner, repo, None)

    if "://" not in u and _GH_SHORT.match(u):
        owner, repo = _GH_SHORT.match(u).groups()
        return _identity(owner.lower(), repo.removesuffix(".git").lower(), None)

    if u.startswith("git+"):
        u = u[4:]

    trial = u if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u) else "https://" + u
    try:
        p = urlparse(trial)
    except Exception:
        return None

    host = (p.hostname or "").lower()
    if host not in GITHUB_HOSTS:
        return None

    path = (p.path or "").rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2:
        return None
    owner, repo = parts[0].lower(), parts[1].lower()
    subpath = None
    if len(parts) >= 4 and parts[2].lower() in _TREE_BLOB:
        subpath = _clean_subpath(parts[4:])
    return _identity(owner, repo, subpath)


def _identity(owner: str, repo: str, subpath: str | None) -> dict[str, Any]:
    repo_url = f"https://github.com/{owner}/{repo}"
    if subpath:
        identity = f"github:{owner}/{repo}::{subpath}"
    else:
        identity = f"github:{owner}/{repo}"
    return {
        "owner": owner,
        "repo": repo,
        "repo_url": repo_url,
        "subpath": subpath,
        "identity": identity,
    }


def normalize_repo_url(raw: str | None) -> str | None:
    parsed = parse_github_source(raw)
    if parsed:
        return parsed["repo_url"]
    if raw is None:
        return None
    u = str(raw).strip()
    if not u:
        return None
    trial = u if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u) else "https://" + u
    try:
        p = urlparse(trial)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    if not host:
        return None
    path = (p.path or "").rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    clean = urlunparse(
        ("https" if p.scheme in {"http", "https"} else p.scheme, host, path, "", "", "")
    )
    return clean.rstrip("/") if path and path != "/" else None


def stable_id_from_repo(norm_repo: str, subpath: str | None = None) -> str:
    if "github.com/" in (norm_repo or ""):
        rest = norm_repo.split("github.com/", 1)[1]
        base = slugify(rest.replace("/", "-"))
    else:
        h = hashlib.sha256((norm_repo or "").encode()).hexdigest()[:10]
        base = slugify(
            urlparse(norm_repo or "").path.strip("/").replace("/", "-") or f"ext-{h}"
        )
    if subpath:
        return slugify(f"{base}-{subpath.replace('/', '-')}")
    return base


def extract_github_urls(title: str, body: str) -> list[str]:
    blob = f"{body or ''}\n{title or ''}"
    found: list[str] = []
    seen: set[str] = set()
    for u in _URL_RE.findall(blob):
        u = u.rstrip(".,;:)")
        if "github.com" not in u.lower() and not u.lower().startswith("git@github.com"):
            continue
        if u not in seen:
            seen.add(u)
            found.append(u)
    return found


def catalog_skill_identity(skill: dict[str, Any]) -> dict[str, Any] | None:
    """Derive identity from a catalog skill record (repo_url + skill_path/subpath)."""
    raw = skill.get("repo_url") or skill.get("repo") or skill.get("github")
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("html_url")
    parsed = parse_github_source(str(raw) if raw else None)
    if not parsed:
        return None
    explicit = skill.get("skill_path") or skill.get("subpath")
    if explicit and isinstance(explicit, str) and explicit.strip():
        sub = explicit.strip().strip("/").lower()
        return _identity(parsed["owner"], parsed["repo"], sub)
    return parsed


def build_catalog_identity_set(catalog: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for s in catalog.get("skills") or []:
        ident = catalog_skill_identity(s)
        if ident:
            ids.add(ident["identity"])
    return ids


def identity_in_catalog(identity: str, catalog_identities: set[str]) -> bool:
    return identity.lower() in {x.lower() for x in catalog_identities}


def load_catalog(path: Path | str | None = None, *, url: str = LIVE_CATALOG_URL) -> dict[str, Any]:
    if path is not None:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": "proskills-operator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to fetch live catalog from {url}: {exc}") from exc


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:24]


def parse_skill_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML-ish frontmatter parser for SKILL.md (key: value only)."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}
    out: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip("'\"")
        if key:
            out[key] = val
    return out


def fetch_raw_text(owner: str, repo: str, path: str, refs: tuple[str, ...] = ("main", "master")) -> str | None:
    for ref in refs:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "proskills-operator/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
    return None


def build_skill_record(
    *,
    owner: str,
    repo: str,
    repo_url: str,
    subpath: str | None,
    skill_md: str | None,
    readme: str | None,
    stars: int | None = None,
    existing_ids: set[str] | None = None,
    existing_slugs: set[str] | None = None,
) -> dict[str, Any]:
    """Build a website-schema skill record for a new listing (append-only)."""
    fm = parse_skill_frontmatter(skill_md or "")
    name = fm.get("name") or fm.get("title") or (Path(subpath).name if subpath else repo)
    description = fm.get("description") or ""
    if not description and skill_md:
        # first non-empty non-heading paragraph after frontmatter
        body = _FRONTMATTER_RE.sub("", skill_md, count=1)
        for line in body.splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                description = t[:500]
                break
    category = (fm.get("category") or "other").lower().replace(" ", "-")
    version = fm.get("version") or "0.0.0"
    preferred = stable_id_from_repo(repo_url, subpath)
    used_ids = existing_ids or set()
    used_slugs = existing_slugs or set()
    sid = preferred
    n = 2
    while sid in used_ids or sid in used_slugs:
        sid = f"{preferred}-{n}"
        n += 1

    readme_text = (readme or skill_md or "")[:8000]
    files_found: list[str] = []
    if skill_md is not None:
        files_found.append("SKILL.md" if not subpath else f"{subpath}/SKILL.md")
    if readme is not None:
        files_found.append("README.md")

    record: dict[str, Any] = {
        "id": sid,
        "slug": sid,
        "name": str(name),
        "category": category,
        "description": description,
        "author": owner,
        "author_url": f"https://github.com/{owner}",
        "version": str(version),
        "repo_url": repo_url,
        "works_with": [],
        "paid": False,
        "price": 0,
        "verified_at": "",
        "featured": False,
        "reviewed": False,
        "is_collection": False,
        "scores": {
            "functionality": 0,
            "documentation": 0,
            "security": 0,
            "maintenance": 0,
            "usefulness": 0,
            "uniqueness": 0,
            "code_quality": 0,
            "average": 0,
        },
        "readme": readme_text,
        "files_found": files_found,
        "github_stars": stars if stars is not None else 0,
    }
    if subpath:
        record["skill_path"] = subpath
    if skill_md is not None:
        record["skill_md"] = skill_md[:8000]
    return record


def gh_api_json(endpoint: str) -> Any:
    """Call `gh api ENDPOINT` and return parsed JSON. Uses GH_TOKEN / gh auth."""
    cmd = ["gh", "api", endpoint]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not found; install GitHub CLI or set PATH") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def gh_api_search_issues(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Search issues via gh api GET search/issues."""
    from urllib.parse import quote

    results: list[dict[str, Any]] = []
    page = 1
    per_page = min(100, max(1, limit))
    while len(results) < limit:
        q = quote(query, safe="")
        endpoint = f"search/issues?q={q}&per_page={per_page}&page={page}"
        data = gh_api_json(endpoint)
        items = (data or {}).get("items") or []
        if not items:
            break
        results.extend(items)
        if len(items) < per_page:
            break
        page += 1
    return results[:limit]


def http_get_json(url: str, timeout: int = 20) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": "proskills-operator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def label_names(issue: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for lab in issue.get("labels") or []:
        if isinstance(lab, dict) and lab.get("name"):
            names.add(str(lab["name"]))
        elif isinstance(lab, str):
            names.add(lab)
    return names


def issue_is_blocked(issue: dict[str, Any]) -> tuple[bool, str | None]:
    num = int(issue.get("number") or 0)
    if num in PROTECTED_ISSUES:
        return True, "protected_issue"
    labs = label_names(issue)
    blocked = labs & BLOCKED_LABELS
    if blocked:
        return True, f"blocked_label:{sorted(blocked)[0]}"
    return False, None
