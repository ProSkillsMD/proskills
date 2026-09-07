"""Deterministic URL / identity helpers for ProSkills catalog remediation."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

GITHUB_HOSTS = {"github.com", "www.github.com"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_GH_SSH = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")
_GH_SHORT = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?$")


def normalize_repo_url(raw: str | None) -> str | None:
    """Return canonical https://github.com/owner/repo or https URL, or None if empty/invalid."""
    if raw is None:
        return None
    u = str(raw).strip()
    if not u or u.lower() in {"none", "null", "undefined", "n/a"}:
        return None

    m = _GH_SSH.match(u)
    if m:
        owner, repo = m.group(1), m.group(2)
        return f"https://github.com/{owner.lower()}/{repo.removesuffix('.git').lower()}"

    # owner/repo shorthand
    if "://" not in u and _GH_SHORT.match(u):
        owner, repo = _GH_SHORT.match(u).groups()
        return f"https://github.com/{owner.lower()}/{repo.removesuffix('.git').lower()}"

    if u.startswith("git+"):
        u = u[4:]

    # ensure scheme for parsing
    trial = u if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u) else "https://" + u
    try:
        p = urlparse(trial)
    except Exception:
        return None

    host = (p.hostname or "").lower()
    if not host:
        return None

    path = (p.path or "").rstrip("/")
    # drop .git
    if path.endswith(".git"):
        path = path[:-4]

    if host in GITHUB_HOSTS:
        parts = [x for x in path.split("/") if x]
        if len(parts) < 2:
            return None
        owner, repo = parts[0].lower(), parts[1].lower()
        # strip github tree/blob extras — keep only owner/repo
        return f"https://github.com/{owner}/{repo}"

    # non-github: scheme + host + path, no query/fragment, lower host
    clean = urlunparse(("https" if p.scheme in {"http", "https"} else p.scheme, host, path, "", "", ""))
    if not path or path == "/":
        return None
    return clean.rstrip("/")


def is_malformed_url(raw: str | None) -> bool:
    if raw is None or not str(raw).strip():
        return False
    return normalize_repo_url(raw) is None


def slugify(text: str, fallback: str = "skill") -> str:
    s = (text or "").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s or s == "undefined":
        s = fallback
    return s[:80].rstrip("-") or fallback


def stable_id_from_repo(norm_repo: str) -> str:
    # github.com/owner/repo -> owner-repo
    if "github.com/" in norm_repo:
        rest = norm_repo.split("github.com/", 1)[1]
        return slugify(rest.replace("/", "-"))
    h = hashlib.sha256(norm_repo.encode()).hexdigest()[:10]
    return slugify(urlparse(norm_repo).path.strip("/").replace("/", "-") or f"ext-{h}")


def stable_id_from_name(name: str, index: int) -> str:
    base = slugify(name or f"skill-{index}")
    return f"{base}-{index}"


def pick_source_fields(row: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (raw_repo, name, raw_category, existing_id/slug hints)."""
    raw_repo = row.get("repo_url") or row.get("repo") or row.get("github") or row.get("repository")
    if isinstance(raw_repo, dict):
        raw_repo = raw_repo.get("url") or raw_repo.get("html_url")
    name = row.get("name") or row.get("title") or ""
    cat = row.get("category")
    if not cat and isinstance(row.get("categories"), list) and row["categories"]:
        cat = row["categories"][0]
    if isinstance(cat, list) and cat:
        cat = cat[0]
    existing_id = row.get("id") if isinstance(row.get("id"), str) else None
    existing_slug = row.get("slug") if isinstance(row.get("slug"), str) else None
    return (
        str(raw_repo).strip() if raw_repo else None,
        str(name).strip() if name else None,
        str(cat).strip() if cat else None,
        (existing_id or existing_slug or None),
    )
