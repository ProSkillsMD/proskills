"""Capture every file in a skill folder for the public catalog.

The catalog ``files`` array is optional and backward compatible. Paths are
repo-root relative, the same strings as ``files_found``. The website builds
``blob/<ref>/<path>`` from that string::

    {"path": "skills/rederive/scripts/history.sh", "size": 3107, "sha": "<git blob sha>",
     "content": "...",          # only when the full text is stored
     "truncated": true,         # text larger than the per-file cap; content absent
     "omitted": true}           # content not stored (binary, symlink, budget, or truncated)

Capture still reads only the skill folder. A path inside that folder is stored
with the folder prefix (``scripts/history.sh`` becomes
``skills/rederive/scripts/history.sh``). ``files_found`` lists every captured
path and keeps a repo-root file outside the folder (``README.md``) when the
row already has it. Inline text is capped at 32 KiB per file and 128 KiB per
skill. ``skill_md`` / ``readme`` stay the legacy 8_000-character fields.

Tree walks use scout's ``MAX_WALK_DIRS`` and, when present, PR #10403's
``MAX_REPO_TREE_CALLS`` / ``TREE_FETCH_BUDGET_S``. HTTP goes through
``scout.GitHubClient``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import quote

import scout
from publish_lib import align_catalog_files, normalize_catalog_path, parse_github_source, parse_skill_frontmatter

# Caps. Per-file inline sits in the requested 20–50 KiB band.
INLINE_MAX_BYTES = 32 * 1024
SKILL_INLINE_MAX_BYTES = 128 * 1024
SKILL_MD_CAP = 8000
README_CAP = 8000
# Largest SKILL.md/README we will download only to refresh the legacy fields.
LEGACY_FETCH_MAX = 256 * 1024

# Fallbacks match scout on main and the caps added by PR #10403. Read live
# values via tree_limits() so a merged #10403 is picked up without editing it.
_MAX_WALK_DIRS = 200
_MAX_REPO_TREE_CALLS = 400
_TREE_FETCH_BUDGET_S = 120.0
_MIN_CORE_REMAINING = 1500
_HTTP_TIMEOUT = 30.0

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BINARY_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".webm", ".wav", ".ogg",
    ".wasm", ".bin", ".exe", ".dll", ".so", ".dylib", ".pyc", ".class", ".jar",
    ".parquet", ".sqlite", ".db", ".npy", ".npz",
})
# Keys a refresh must not take from upstream file capture.
LOCKED_FIELDS = (
    "id", "slug", "author", "author_url", "license", "license_spdx", "attribution",
    "repo_url", "source_url", "source_type", "category", "scores", "featured", "reviewed",
    "paid", "price", "verified_at", "works_with", "is_collection", "github_stars",
    "external_ratings",
)
EXCLUDED = scout.EXCLUDED_SEGMENTS
RAW = scout.RAW


def tree_limits() -> dict[str, float]:
    """Walk and HTTP ceilings. Prefer scout's constants when present (PR #10403)."""
    defaults = getattr(scout, "DEFAULTS", {}) or {}
    return {
        "max_dirs": int(getattr(scout, "MAX_WALK_DIRS", _MAX_WALK_DIRS)),
        "max_calls": int(getattr(scout, "MAX_REPO_TREE_CALLS", _MAX_REPO_TREE_CALLS)),
        "budget_s": float(getattr(scout, "TREE_FETCH_BUDGET_S", _TREE_FETCH_BUDGET_S)),
        "min_core": int(defaults.get("min_core_remaining", _MIN_CORE_REMAINING)),
        "http_timeout": float(getattr(scout, "GITHUB_HTTP_TIMEOUT", _HTTP_TIMEOUT)),
    }


def make_client(token: str | None = None) -> scout.GitHubClient:
    """GitHub client with the shared HTTP timeout. The token is never logged."""
    limits = tree_limits()
    if token is None:
        token = scout.get_gh_token()
    return scout.GitHubClient(token, timeout=limits["http_timeout"], throttle=scout.AdaptiveThrottle(4))


def git_blob_sha(data: bytes) -> str:
    """Git blob object id (sha1 of ``blob <len>\\0`` + bytes)."""
    digest = hashlib.sha1()
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def policy_hits(*texts: Any) -> list[str]:
    """Content-policy matches (adult/NSFW, piracy, illegal). Empty if the text is clean.

    Uses ``content_policy.matches`` — the same matcher as the open content-policy
    hold (``hold:content-policy``). A refresh that trips it is flagged, not written.
    """
    import content_policy as CP
    return CP.matches(*texts)


def skill_relative(path: str | None) -> str | None:
    """Skill-folder-relative posix path, or None if it escapes the folder."""
    if not isinstance(path, str):
        return None
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or "\x00" in part:
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def normalize_folder(folder: str | None) -> str | None:
    if folder is None:
        return None
    rel = skill_relative(str(folder).strip().strip("/"))
    if rel is None:
        return None
    if rel.lower() == "skill.md":
        return None
    if rel.lower().endswith("/skill.md"):
        parent = rel[: -(len("skill.md") + 1)]
        return skill_relative(parent) if parent else None
    return rel


def folder_of_skill(skill: dict[str, Any]) -> str | None:
    explicit = skill.get("skill_path") or skill.get("subpath")
    if not explicit:
        return None
    return normalize_folder(str(explicit))


def owner_repo_of(skill: dict[str, Any]) -> tuple[str, str] | None:
    if str(skill.get("source_type") or "") == "clawhub":
        return None
    raw = str(skill.get("repo_url") or "")
    if "clawhub.ai" in raw or "clawhub.com" in raw:
        return None
    parsed = parse_github_source(raw)
    if not parsed:
        return None
    return str(parsed["owner"]), str(parsed["repo"])


def repo_join(folder: str | None, rel: str) -> str | None:
    """Repo path of ``rel`` inside ``folder``, or None if it would leave the folder."""
    rel_n = skill_relative(rel)
    if rel_n is None:
        return None
    if not folder:
        return rel_n
    folder_n = normalize_folder(folder)
    if folder_n is None:
        return None
    full = f"{folder_n}/{rel_n}"
    if not full.startswith(folder_n + "/"):
        return None
    if skill_relative(full) != full:
        return None
    return full


def _excluded(rel: str) -> bool:
    return any(part.lower() in EXCLUDED for part in rel.split("/") if part)


def _priority(path: str) -> tuple:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    if name == "skill.md":
        rank = 0
    elif name == "readme.md":
        rank = 1
    elif low.startswith("scripts/"):
        rank = 2
    elif low.startswith("references/"):
        rank = 3
    elif low.startswith("assets/"):
        rank = 4
    else:
        rank = 5
    return (rank, low)


def _is_binary(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _blob_entry(rel: str, ent: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": rel,
        "size": int(ent.get("size") or 0),
        "sha": str(ent.get("sha") or ""),
        "symlink": str(ent.get("mode") or "") == "120000",
    }


def _iter_dir(entries: list, prefix: str):
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("path") or "")
        if not name or "/" in name or name in (".", ".."):
            continue
        rel = skill_relative(f"{prefix}/{name}" if prefix else name)
        if rel is None or _excluded(rel):
            continue
        yield rel, ent


def _blobs_from_recursive(tree: list) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ent in tree:
        if not isinstance(ent, dict) or ent.get("type") != "blob":
            continue
        rel = skill_relative(str(ent.get("path") or ""))
        if rel is None or _excluded(rel) or rel in seen:
            continue
        seen.add(rel)
        files.append(_blob_entry(rel, ent))
    return files


def _bounded_children(
    client: Any,
    owner: str,
    repo: str,
    initial: list,
    *,
    limits: dict[str, float],
    clock: Callable[[], float],
    calls_already: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Non-recursive walk that never enters excluded directories.

    ``initial`` is the already-fetched listing of the skill folder root.
    Child fetches stop at the directory cap, the tree-call cap, or the time budget.
    """
    deadline = clock() + float(limits["budget_s"])
    calls = calls_already
    dirs = 1
    files: list[dict[str, Any]] = []
    queue: list[tuple[str, str]] = []
    truncated = False
    reason: str | None = None

    def consume(entries: list, prefix: str) -> None:
        for rel, ent in _iter_dir(entries, prefix):
            kind = ent.get("type")
            if kind == "tree":
                sha = ent.get("sha")
                if sha:
                    queue.append((str(sha), rel))
            elif kind == "blob":
                files.append(_blob_entry(rel, ent))

    consume(initial, "")
    while queue:
        if clock() >= deadline:
            truncated, reason = True, "time_budget"
            break
        if calls >= int(limits["max_calls"]):
            truncated, reason = True, "tree_call_cap"
            break
        if dirs >= int(limits["max_dirs"]):
            truncated, reason = True, "walk_dir_cap"
            break
        sha, prefix = queue.pop()
        dirs += 1
        calls += 1
        data = client.rest(f"repos/{owner}/{repo}/git/trees/{quote(sha, safe='')}") or {}
        if data.get("truncated"):
            truncated, reason = True, "truncated_listing"
        consume(data.get("tree") or [], prefix)
    return files, truncated, reason


def list_skill_tree(
    client: Any,
    owner: str,
    repo: str,
    folder: str | None,
    ref: str,
    *,
    limits: dict[str, float] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """List blobs inside the skill folder only.

    A subfolder uses ``git/trees/{ref}:{folder}``. A root skill walks from the
    repo root and does not enter ``node_modules`` and the other excluded dirs.
    Raises the scout GitHub errors on transport failure.
    """
    limits = limits or tree_limits()
    clock = clock or time.monotonic
    folder_n = normalize_folder(folder)
    if folder_n:
        spec = f"{quote(ref, safe='')}:{quote(folder_n, safe='/')}?recursive=1"
        data = client.rest(f"repos/{owner}/{repo}/git/trees/{spec}") or {}
        if not data.get("truncated"):
            return {
                "files": _blobs_from_recursive(data.get("tree") or []),
                "truncated": False,
                "truncated_reason": None,
                "ref": ref,
                "folder": folder_n,
            }
        # Discard the partial recursive body and walk only this folder tree.
        sha = str(data.get("sha") or "")
        if not sha:
            return {
                "files": [],
                "truncated": True,
                "truncated_reason": "truncated_listing",
                "ref": ref,
                "folder": folder_n,
            }
        root = client.rest(f"repos/{owner}/{repo}/git/trees/{quote(sha, safe='')}") or {}
        files, truncated, reason = _bounded_children(
            client, owner, repo, root.get("tree") or [],
            limits=limits, clock=clock, calls_already=2,
        )
        if root.get("truncated"):
            truncated, reason = True, reason or "truncated_listing"
        return {
            "files": files,
            "truncated": truncated,
            "truncated_reason": reason,
            "ref": ref,
            "folder": folder_n,
        }

    root = client.rest(f"repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}") or {}
    files, truncated, reason = _bounded_children(
        client, owner, repo, root.get("tree") or [],
        limits=limits, clock=clock, calls_already=1,
    )
    if root.get("truncated"):
        truncated, reason = True, reason or "truncated_listing"
    return {
        "files": files,
        "truncated": truncated,
        "truncated_reason": reason,
        "ref": ref,
        "folder": None,
    }


def _known_text(text: str | None, size: int, sha: str) -> str | None:
    if not isinstance(text, str) or not _SHA_RE.fullmatch(sha or ""):
        return None
    data = text.encode("utf-8")
    if len(data) != size or git_blob_sha(data) != sha:
        return None
    return text


def fetch_blob_bytes(client: Any, owner: str, repo: str, ref: str, folder: str | None, rel: str) -> bytes:
    """Download one blob. Refuses any path that is not inside the skill folder."""
    full = repo_join(folder, rel)
    if full is None:
        raise ValueError("refusing path outside skill folder")
    url = f"{RAW}/{quote(owner)}/{quote(repo)}/{quote(ref, safe='')}/{quote(full)}"
    resp = client.request(url, auth=False, accept="*/*")
    body = getattr(resp, "body", b"")
    return body if isinstance(body, bytes) else bytes(body)


def assemble_files(
    entries: list[dict[str, Any]],
    fetch: Callable[[str], bytes | None],
    *,
    inline_max: int = INLINE_MAX_BYTES,
    total_max: int = SKILL_INLINE_MAX_BYTES,
    known_texts: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Turn tree entries into catalog file objects plus legacy SKILL/README text.

    ``fetch`` is called only for files inside the folder that might be inlined
    or that are a SKILL.md/README small enough to refresh the legacy fields.
    The returned legacy map holds the full decoded text (not the 8_000 cap).
    """
    known = known_texts or {}
    budget = int(total_max)
    files: list[dict[str, Any]] = []
    legacy: dict[str, str] = {}
    seen: set[str] = set()
    for ent in sorted(entries, key=lambda e: _priority(str(e.get("path") or ""))):
        rel = skill_relative(str(ent.get("path") or ""))
        if rel is None or rel in seen or _excluded(rel):
            continue
        seen.add(rel)
        size = int(ent.get("size") or 0)
        sha = str(ent.get("sha") or "")
        item: dict[str, Any] = {"path": rel, "size": size, "sha": sha}
        ext = PurePosixPath(rel).suffix.lower()
        legacy_name = rel.lower() in {"skill.md", "readme.md"}
        if ent.get("symlink") or not _SHA_RE.fullmatch(sha):
            item["omitted"] = True
            files.append(item)
            continue
        if ext in BINARY_EXT:
            item["omitted"] = True
            files.append(item)
            continue
        want_body = size <= inline_max or (legacy_name and size <= LEGACY_FETCH_MAX)
        if not want_body:
            item["truncated"] = True
            item["omitted"] = True
            files.append(item)
            continue
        text = _known_text(known.get(rel) or known.get(rel.lower()), size, sha)
        if text is None:
            data = fetch(rel)
            if data is None or _is_binary(data):
                item["omitted"] = True
                files.append(item)
                continue
            text = data.decode("utf-8")
        if legacy_name:
            legacy[rel.lower()] = text
        encoded = len(text.encode("utf-8"))
        if encoded > inline_max or size > inline_max:
            item["truncated"] = True
            item["omitted"] = True
            files.append(item)
            continue
        if encoded > budget:
            item["omitted"] = True
            files.append(item)
            continue
        item["content"] = text
        budget -= encoded
        files.append(item)
    return files, legacy


def _stop_reason(exc: BaseException) -> str | None:
    if isinstance(exc, scout.RateLimitExhausted):
        return "rate_limit"
    if isinstance(exc, scout.Transient):
        return "transient"
    if isinstance(exc, (scout.NotFound, scout.Unprocessable)):
        return "not_found"
    return None


def capture_skill_folder(
    client: Any,
    owner: str,
    repo: str,
    folder: str | None,
    ref: str,
    *,
    inline_max: int = INLINE_MAX_BYTES,
    total_max: int = SKILL_INLINE_MAX_BYTES,
    known_texts: dict[str, str] | None = None,
    limits: dict[str, float] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Capture one skill folder. ``ok`` False carries ``reason`` and no files.

    Transport failures are returned, not raised, so a backfill can resume.
    """
    try:
        listing = list_skill_tree(client, owner, repo, folder, ref, limits=limits, clock=clock)
    except Exception as exc:  # noqa: BLE001 — classified below; unknown errors propagate
        reason = _stop_reason(exc)
        if reason is None:
            raise
        return {"ok": False, "reason": reason, "ref": ref}

    def fetch(rel: str) -> bytes | None:
        try:
            return fetch_blob_bytes(client, owner, repo, ref, listing.get("folder"), rel)
        except scout.NotFound:
            return None

    try:
        files, legacy = assemble_files(
            listing["files"], fetch, inline_max=inline_max, total_max=total_max, known_texts=known_texts,
        )
    except Exception as exc:  # noqa: BLE001
        reason = _stop_reason(exc)
        if reason is None:
            raise
        return {"ok": False, "reason": reason, "ref": ref}

    skill_text = legacy.get("skill.md")
    readme_text = legacy.get("readme.md")
    has_skill = any(f["path"].lower() == "skill.md" for f in files)
    return {
        "ok": True,
        "files": files,
        "truncated": bool(listing.get("truncated")),
        "truncated_reason": listing.get("truncated_reason"),
        "skill_md": skill_text,
        "readme": readme_text,
        "ref": ref,
        "folder": listing.get("folder"),
        "missing_skill_md": not has_skill and not listing.get("truncated"),
    }


def capture_with_refs(
    client: Any,
    owner: str,
    repo: str,
    folder: str | None,
    preferred: str | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Try the repo default branch, then main, then master. Stops on a real hit or a rate limit."""
    refs: list[str] = []
    if not preferred:
        try:
            meta = client.rest(f"repos/{owner}/{repo}") or {}
        except Exception as exc:  # noqa: BLE001
            reason = _stop_reason(exc)
            if reason is None:
                raise
            return {"ok": False, "reason": reason}
        branch = meta.get("default_branch")
        if isinstance(branch, str) and branch.strip():
            refs.append(branch.strip())
    for ref in (preferred, "main", "master"):
        if isinstance(ref, str) and ref.strip() and ref.strip() not in refs:
            refs.append(ref.strip())
    last: dict[str, Any] = {"ok": False, "reason": "not_found"}
    for ref in refs:
        result = capture_skill_folder(client, owner, repo, folder, ref, **kwargs)
        if result.get("ok") or result.get("reason") != "not_found":
            return result
        last = result
    return last


def files_from_docs(
    skill_md: str | None,
    readme: str | None,
    *,
    include_readme: bool,
    inline_max: int = INLINE_MAX_BYTES,
) -> list[dict[str, Any]]:
    """File entries for text we already hold. Used when the tree was not listed.

    The README is included only when it lives in the skill folder. A repo-root
    README used as the legacy ``readme`` blurb is not stored as a skill file.
    """
    entries: list[dict[str, Any]] = []
    known: dict[str, str] = {}
    if skill_md is not None:
        data = skill_md.encode("utf-8")
        entries.append({"path": "SKILL.md", "size": len(data), "sha": git_blob_sha(data)})
        known["SKILL.md"] = skill_md
    if include_readme and readme is not None:
        data = readme.encode("utf-8")
        entries.append({"path": "README.md", "size": len(data), "sha": git_blob_sha(data)})
        known["README.md"] = readme

    def fetch(rel: str) -> bytes | None:
        text = known.get(rel)
        return text.encode("utf-8") if text is not None else None

    files, _legacy = assemble_files(entries, fetch, inline_max=inline_max, known_texts=known)
    return files


def merge_refresh(existing: dict[str, Any], captured: dict[str, Any]) -> dict[str, Any]:
    """Copy file capture onto a skill without touching attribution or license.

    ``id``, ``slug``, ``author``, ``repo_url``, ``category``, and an existing
    ``license`` stay as they are. A missing license may be filled from SKILL.md
    frontmatter. ``description`` and ``version`` follow the upstream frontmatter
    when it has them. ``skill_md`` is refreshed (8_000-character legacy cap).
    """
    updated = dict(existing)
    folder = folder_of_skill(existing)
    files, files_found = align_catalog_files(
        list(captured.get("files") or []), folder, existing.get("files_found"),
    )
    updated["files"] = files
    updated["files_found"] = files_found
    updated.pop("files_truncated", None)
    skill_md = captured.get("skill_md")
    if isinstance(skill_md, str):
        updated["skill_md"] = skill_md[:SKILL_MD_CAP]
    readme = captured.get("readme")
    if isinstance(readme, str):
        updated["readme"] = readme[:README_CAP]
    fm = parse_skill_frontmatter(skill_md or "")
    if fm.get("description"):
        updated["description"] = str(fm["description"])[:500]
    if fm.get("version"):
        updated["version"] = str(fm["version"])[:40]
    for key in LOCKED_FIELDS:
        if key == "license":
            continue
        if key in existing:
            updated[key] = existing[key]
    if existing.get("license"):
        updated["license"] = existing["license"]
    elif fm.get("license"):
        updated["license"] = str(fm["license"])[:120]
    if existing.get("attribution"):
        updated["attribution"] = existing["attribution"]
    return updated


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def refresh_skill(existing: dict[str, Any], captured: dict[str, Any] | None) -> dict[str, Any]:
    """Compare upstream SKILL.md and blob SHAs with the stored skill.

    Returns ``{skill, status, ...}``. ``status`` is ``updated``, ``unchanged``,
    ``flag_review``, ``truncated``, or ``capture_failed``. The returned skill is
    the stored record whenever we refuse to write (policy, truncated tree,
    missing SKILL.md, or no upstream change). Applying the result twice is a no-op.
    """
    if not captured or not captured.get("ok"):
        return {
            "skill": existing,
            "status": "capture_failed",
            "reason": (captured or {}).get("reason") or "capture_failed",
        }
    if captured.get("truncated"):
        return {
            "skill": existing,
            "status": "truncated",
            "reason": captured.get("truncated_reason") or "truncated",
        }
    if captured.get("missing_skill_md"):
        return {"skill": existing, "status": "flag_review", "reason": "missing_skill_md", "hits": []}
    texts: list[Any] = [
        existing.get("name"), existing.get("description"), existing.get("slug"),
        captured.get("skill_md"), captured.get("readme"),
    ]
    for entry in captured.get("files") or []:
        texts.append(entry.get("path"))
        if "content" in entry:
            texts.append(entry.get("content"))
    hits = policy_hits(*texts)
    if hits:
        return {"skill": existing, "status": "flag_review", "reason": "content_policy", "hits": hits}
    merged = merge_refresh(existing, captured)
    if _canon(merged) == _canon(existing):
        return {"skill": existing, "status": "unchanged"}
    return {"skill": merged, "status": "updated"}


def load_ranking(path: Any) -> dict[str, float]:
    """Load ``slug -> views`` from a CSV (``slug,views``) or JSON list/object."""
    text = path.read_text(encoding="utf-8") if hasattr(path, "read_text") else str(path)
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped[0] in "[{":
        data = json.loads(stripped)
        rows: list[dict[str, Any]] = []
        if isinstance(data, dict) and "skills" in data and isinstance(data["skills"], list):
            rows = [r for r in data["skills"] if isinstance(r, dict)]
        elif isinstance(data, dict):
            out: dict[str, float] = {}
            for key, val in data.items():
                if key == "skills":
                    continue
                try:
                    out[str(key)] = float(val)
                except (TypeError, ValueError):
                    continue
            return out
        elif isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        ranking: dict[str, float] = {}
        for row in rows:
            slug = row.get("slug") or row.get("id")
            if not slug:
                continue
            try:
                ranking[str(slug)] = float(row.get("views") if row.get("views") is not None else row.get("view") or 0)
            except (TypeError, ValueError):
                ranking[str(slug)] = 0.0
        return ranking
    import csv
    from io import StringIO
    reader = csv.DictReader(StringIO(text))
    ranking = {}
    for row in reader:
        lowered = {str(k or "").strip().lower(): v for k, v in row.items()}
        slug = (lowered.get("slug") or lowered.get("id") or "").strip()
        if not slug:
            continue
        try:
            ranking[slug] = float(str(lowered.get("views") or lowered.get("view") or "0").strip() or 0)
        except ValueError:
            ranking[slug] = 0.0
    return ranking


def order_skills(skills: list[dict[str, Any]], ranking: dict[str, float]) -> list[dict[str, Any]]:
    """Highest views first. Skills missing from the ranking follow, by stars then slug.

    The result is a list of ``{slug, tier, views, github_stars}`` in processing order.
    """
    ranked: list[tuple] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        slug = str(skill.get("slug") or skill.get("id") or "").strip()
        if not slug:
            continue
        stars = int(skill.get("github_stars") or 0)
        if slug in ranking:
            ranked.append((0, -float(ranking[slug]), slug, "ranked", ranking[slug], stars))
        else:
            ranked.append((1, -stars, slug, "unranked", None, stars))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    return [
        {"slug": slug, "tier": tier, "views": views, "github_stars": stars}
        for _tier_n, _score, slug, tier, views, stars in ranked
    ]


def order_fingerprint(order: list[str]) -> str:
    """Identity of one traffic order. Recorded in state; resume does not use it."""
    return hashlib.sha256("\n".join(order).encode("utf-8")).hexdigest()[:16]


def _cursor_int(value: Any) -> int:
    try:
        cursor = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return cursor if cursor > 0 else 0


def completed_slugs(data: dict[str, Any]) -> set[str]:
    """Slugs already done on this pass.

    ``completed`` is the resume key. Older files also stored a cursor index
    into ``order``; that prefix counts as done so a half-written map still
    resumes. A finished pass starts clean, so it contributes nothing.
    """
    if data.get("finished"):
        return set()
    done: set[str] = set()
    completed = data.get("completed")
    if isinstance(completed, dict):
        done.update(str(slug) for slug in completed if str(slug))
    elif isinstance(completed, list):
        done.update(str(slug) for slug in completed if str(slug))
    saved = [str(slug) for slug in (data.get("order") or []) if str(slug)]
    for slug in saved[:_cursor_int(data.get("cursor"))]:
        done.add(slug)
    return done


class BackfillState:
    """Resumable progress under ``operator/state``.

    A finished pass starts clean. A killed one continues. Progress is the set
    of completed slugs: a newly published row changes the traffic order and
    must not send the cursor back to the first skills.
    """

    VERSION = 1

    def __init__(self, path: Any):
        self.path = path
        self.data: dict[str, Any] = self._empty("")

    def _empty(self, fingerprint: str) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "order_fingerprint": fingerprint,
            "cursor": 0,
            "finished": False,
            "order": [],
            "completed": {},
            "flagged": {},
            "errors": {},
            "updated": [],
            "unchanged": [],
            "pending": {},
        }

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict) and raw.get("version") == self.VERSION:
            self.data.update(raw)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def begin(self, order: list[str]) -> int:
        """Prepare ``order`` and return the index of the first slug not yet done.

        A finished pass starts at 0 with an empty set. Otherwise slugs already
        in ``completed``, or implied by an older cursor into the saved order,
        stay done. The new order is stored only after that prefix is absorbed,
        and the cursor is cleared so it cannot mark a newly inserted row done.
        """
        fp = order_fingerprint(order)
        if self.data.get("finished"):
            self.data = self._empty(fp)
            self.data["order"] = list(order)
            return 0
        self._absorb_legacy_cursor()
        self.data["cursor"] = 0
        self.data["order"] = list(order)
        self.data["order_fingerprint"] = fp
        return _peek_cursor(self, order)

    def _absorb_legacy_cursor(self) -> None:
        """Fold a pre-completed cursor prefix into ``completed`` without clobbering statuses."""
        completed = self.data.get("completed")
        if isinstance(completed, list):
            completed = {str(slug): "resumed" for slug in completed if str(slug)}
            self.data["completed"] = completed
        elif not isinstance(completed, dict):
            completed = {}
            self.data["completed"] = completed
        saved = [str(slug) for slug in (self.data.get("order") or []) if str(slug)]
        for slug in saved[:_cursor_int(self.data.get("cursor"))]:
            completed.setdefault(slug, "resumed")

    def pending_slugs(self, order: list[str]) -> list[str]:
        done = completed_slugs(self.data)
        return [slug for slug in order if slug not in done]

    def note(self, index: int, slug: str, status: str, *, hits: list[str] | None = None,
             reason: str | None = None, skill: dict[str, Any] | None = None) -> None:
        self.data["cursor"] = index + 1
        self.data["completed"][slug] = status
        if status == "updated" and skill is not None:
            self.data.setdefault("updated", []).append(slug)
            self.data.setdefault("pending", {})[slug] = skill
        elif status == "unchanged":
            self.data.setdefault("unchanged", []).append(slug)
            (self.data.get("pending") or {}).pop(slug, None)
        elif status == "flag_review":
            self.data.setdefault("flagged", {})[slug] = {
                "action": "review",
                "reason": reason or "content_policy",
                "hits": list(hits or []),
            }
            (self.data.get("pending") or {}).pop(slug, None)

    def clear_pending(self) -> None:
        self.data["pending"] = {}
        order = [str(slug) for slug in (self.data.get("order") or []) if str(slug)]
        done = self.data.get("completed") if isinstance(self.data.get("completed"), dict) else {}
        if order and all(slug in done for slug in order):
            self.data["finished"] = True
        self.save()


def diff_bytes(before: dict[str, Any], after: dict[str, Any]) -> int:
    """UTF-8 byte delta of the catalog JSON the publisher writes."""
    from publish_lib import catalog_json_dumps
    old = (catalog_json_dumps(before) + "\n").encode("utf-8")
    new = (catalog_json_dumps(after) + "\n").encode("utf-8")
    return len(new) - len(old)


def _index(skills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        slug = str(skill.get("slug") or skill.get("id") or "").strip()
        if slug and slug not in out:
            out[slug] = skill
    return out


def _apply_results(catalog: dict[str, Any], replaced: dict[str, dict[str, Any]]) -> dict[str, Any]:
    skills = []
    for skill in catalog.get("skills") or []:
        slug = str(skill.get("slug") or skill.get("id") or "")
        skills.append(replaced.get(slug, skill))
    out = dict(catalog)
    out["skills"] = skills
    if "total" in out:
        out["total"] = len(skills)
    return out


def process_skill(skill: dict[str, Any], capture: Callable[..., dict[str, Any]], *, preferred_ref: str | None = None) -> dict[str, Any]:
    """Capture and refresh one skill. ``capture(owner, repo, folder, ref)`` returns a capture dict."""
    located = owner_repo_of(skill)
    if located is None:
        return {"slug": str(skill.get("slug") or ""), "status": "skipped", "reason": "no_github", "skill": skill}
    owner, repo = located
    folder = folder_of_skill(skill)
    ref = preferred_ref or skill.get("default_branch")
    if not isinstance(ref, str):
        ref = None
    captured = capture(owner, repo, folder, ref)
    outcome = refresh_skill(skill, captured)
    outcome["slug"] = str(skill.get("slug") or skill.get("id") or "")
    outcome["captured"] = captured
    return outcome


def run_backfill(
    catalog: dict[str, Any],
    ranking: dict[str, float],
    *,
    state: BackfillState | None = None,
    apply: bool = False,
    batch: int = 10,
    remaining: int = 100,
    capture: Callable[..., dict[str, Any]] | None = None,
    preview_slugs: list[str] | None = None,
    client: Any = None,
    show_content: bool = False,
) -> dict[str, Any]:
    """Order the catalog and, on apply, refresh up to ``batch`` skills serially.

    Dry-run does not write ``state`` and does not fetch unless ``preview_slugs``
    is set. Those previews produce ``diff_bytes``. Apply saves the state file
    after every skill so a killed process resumes at the next one.
    """
    worker = capture
    if worker is None:
        if client is None:
            def worker(owner: str, repo: str, folder: str | None, ref: str | None) -> dict[str, Any]:
                return {"ok": False, "reason": "no_client"}
        else:
            def worker(owner: str, repo: str, folder: str | None, ref: str | None, _client=client) -> dict[str, Any]:
                return capture_with_refs(_client, owner, repo, folder, ref)

    skills = [s for s in (catalog.get("skills") or []) if isinstance(s, dict)]
    ordered = order_skills(skills, ranking)
    slugs = [row["slug"] for row in ordered]
    by_slug = _index(skills)
    if state is None:
        pending = list(slugs)
        cursor = 0
    elif apply:
        cursor = state.begin(slugs)
        state.save()
        pending = state.pending_slugs(slugs)
    else:
        pending = state.pending_slugs(slugs)
        cursor = _peek_cursor(state, slugs)
    inspect_limit = max(0, min(int(batch), int(remaining)))
    results: list[dict[str, Any]] = []
    replaced: dict[str, dict[str, Any]] = {}
    stopped: str | None = None
    # Captures saved by a killed run are republished before any new fetch.
    if apply and state is not None:
        for slug, skill in (state.data.get("pending") or {}).items():
            if isinstance(skill, dict):
                replaced[str(slug)] = skill
    room = max(0, inspect_limit - len(replaced))
    # Skip completed slugs wherever they sit. A new row can land in front of
    # the old cursor; slicing the order from that index would repeat work.
    window = pending[:room] if apply else []
    positions = {}
    for index, slug in enumerate(slugs):
        positions.setdefault(slug, index)

    def handle(slug: str, *, persist: bool, index: int | None) -> dict[str, Any]:
        nonlocal stopped
        skill = by_slug.get(slug)
        if skill is None:
            outcome = {"slug": slug, "status": "skipped", "reason": "not_in_catalog", "skill": None}
        else:
            if client is not None and _core_low(client):
                stopped = "core_remaining"
                return {"slug": slug, "status": "stopped", "reason": "core_remaining", "skill": skill}
            outcome = process_skill(skill, worker)
        status = outcome["status"]
        reason = outcome.get("reason")
        if status == "capture_failed" and reason in {"rate_limit", "transient"}:
            prior = int((state.data.get("errors") or {}).get(slug, 0)) if state is not None else 0
            if prior < 1:
                if persist and state is not None:
                    state.data.setdefault("errors", {})[slug] = prior + 1
                    state.save()
                stopped = reason
                outcome["status"] = "stopped"
                return outcome
            status = "capture_failed"
            outcome["status"] = status
        if status == "updated" and outcome.get("skill") is not None:
            replaced[slug] = outcome["skill"]
        if persist and state is not None and index is not None and outcome["status"] != "stopped":
            state.note(
                index, slug, outcome["status"], hits=outcome.get("hits"), reason=outcome.get("reason"),
                skill=outcome.get("skill") if outcome["status"] == "updated" else None,
            )
            state.save()
        return outcome

    if apply:
        for slug in window:
            if stopped:
                break
            if len(replaced) >= inspect_limit or len(replaced) >= int(remaining):
                break
            results.append(handle(slug, persist=True, index=positions[slug]))
            if stopped:
                break
    else:
        for slug in preview_slugs or []:
            if slug not in by_slug and slug not in slugs:
                results.append({"slug": slug, "status": "skipped", "reason": "not_in_catalog"})
                continue
            results.append(handle(slug, persist=False, index=None))

    after = _apply_results(catalog, replaced) if replaced else catalog
    public_results = [_public_outcome(row, show_content=show_content) for row in results]
    return {
        "order": [{k: v for k, v in row.items()} for row in ordered],
        "cursor": cursor,
        "planned_this_run": window if apply else pending[:inspect_limit],
        "results": public_results,
        "updated": list(replaced),
        "flagged": [row["slug"] for row in results if row.get("status") == "flag_review"],
        "unchanged": [row["slug"] for row in results if row.get("status") == "unchanged"],
        "stopped": stopped,
        "diff_bytes": diff_bytes(catalog, after),
        "catalog": after,
        "changed": len(replaced),
    }


def _peek_cursor(state: BackfillState, slugs: list[str]) -> int:
    """Index of the first slug that is not already completed. Does not write state."""
    done = completed_slugs(state.data)
    for index, slug in enumerate(slugs):
        if slug not in done:
            return index
    return len(slugs)


def _core_low(client: Any) -> bool:
    remaining = getattr(client, "core_remaining", None)
    if remaining is None:
        return False
    return int(remaining) < int(tree_limits()["min_core"])


_REPO_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.I)
_TREE_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/(?:blob|tree)/([^/]+)(/.*)?$", re.I,
)


def website_upstream_url(skill: dict[str, Any], path: str) -> str | None:
    """``blob/<ref>/<path>`` link from ``lib/skill-files.mjs``.

    ``path`` is repo-root relative. ``ref`` comes from ``source_url`` when it
    names the same repository, otherwise ``HEAD``.
    """
    path_n = normalize_catalog_path(path)
    if not path_n:
        return None
    repo = _REPO_URL_RE.match(str(skill.get("repo_url") or "").strip())
    src = _TREE_URL_RE.match(str(skill.get("source_url") or "").strip())
    owner = (repo.group(1) if repo else None) or (src.group(1) if src else None)
    name = (repo.group(2) if repo else None) or (src.group(2) if src else None)
    if not owner or not name:
        return None
    ref = "HEAD"
    if src and src.group(1).lower() == owner.lower() and src.group(2).lower() == name.lower():
        ref = src.group(3)
    encoded = "/".join(quote(part, safe="") for part in path_n.split("/"))
    return f"https://github.com/{owner}/{name}/blob/{quote(ref, safe='')}/{encoded}"


def website_file_list(skill: dict[str, Any]) -> list[dict[str, Any]]:
    """File rows the skill page renders: ``files_found`` first, then extra ``files``.

    A ``files`` entry contributes content only when its path matches. ``SKILL.md``
    with no inline bytes falls back to ``skill_md``. Duplicate paths collapse.
    """
    by_path: dict[str, dict[str, Any]] = {}
    for entry in skill.get("files") or []:
        if not isinstance(entry, dict):
            continue
        path = normalize_catalog_path(entry.get("path") if isinstance(entry.get("path"), str) else None)
        if path and path not in by_path:
            by_path[path] = entry
    ordered: list[str] = []
    seen: set[str] = set()
    for item in skill.get("files_found") or []:
        path = normalize_catalog_path(item if isinstance(item, str) else None)
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    for path in by_path:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    rows: list[dict[str, Any]] = []
    for path in ordered:
        entry = by_path.get(path)
        content = entry.get("content") if isinstance(entry, dict) and isinstance(entry.get("content"), str) else None
        if content is None and path.rsplit("/", 1)[-1].lower() == "skill.md" and isinstance(skill.get("skill_md"), str):
            content = skill["skill_md"]
        if isinstance(content, str) and "\x00" in content:
            content = None
        rows.append({
            "path": path,
            "content": content,
            "upstream": website_upstream_url(skill, path),
            "size": entry.get("size") if isinstance(entry, dict) else None,
            "sha": entry.get("sha") if isinstance(entry, dict) else None,
        })
    return rows


def _public_outcome(row: dict[str, Any], *, show_content: bool = False) -> dict[str, Any]:
    skill = row.get("skill") if isinstance(row.get("skill"), dict) else None
    listed = skill.get("files") if skill and isinstance(skill.get("files"), list) else None
    if not listed:
        listed = (row.get("captured") or {}).get("files") or []
    files = []
    for entry in listed:
        pub = {
            "path": entry.get("path"),
            "size": entry.get("size"),
            "sha": entry.get("sha"),
            "content_present": "content" in entry,
        }
        if "content" in entry:
            pub["content_bytes"] = len(str(entry["content"]).encode("utf-8"))
            if show_content:
                pub["content"] = entry["content"]
            else:
                pub["content_preview"] = str(entry["content"])[:120]
        if entry.get("truncated"):
            pub["truncated"] = True
        if entry.get("omitted"):
            pub["omitted"] = True
        files.append(pub)
    out: dict[str, Any] = {
        "slug": row.get("slug"),
        "status": row.get("status"),
    }
    if row.get("reason"):
        out["reason"] = row["reason"]
    if row.get("hits"):
        out["hits"] = row["hits"]
    if files:
        out["files"] = files
    if skill and isinstance(skill.get("files_found"), list):
        out["files_found"] = list(skill["files_found"])
    if skill:
        rendered = []
        for item in website_file_list(skill):
            rendered.append({
                "path": item["path"],
                "content_present": bool(item.get("content")),
                "upstream": item.get("upstream"),
            })
        if rendered:
            out["rendered"] = rendered
    return out


def file_summary(files: list[dict[str, Any]], *, show_content: bool = False) -> list[dict[str, Any]]:
    """JSON-safe view of a ``files`` array. Content is included only when asked."""
    out = []
    for entry in files:
        pub: dict[str, Any] = {
            "path": entry.get("path"),
            "size": entry.get("size"),
            "sha": entry.get("sha"),
            "content_present": "content" in entry,
        }
        if entry.get("truncated"):
            pub["truncated"] = True
        if entry.get("omitted"):
            pub["omitted"] = True
        if "content" in entry:
            pub["content_bytes"] = len(str(entry["content"]).encode("utf-8"))
            if show_content:
                pub["content"] = entry["content"]
            else:
                pub["content_preview"] = str(entry["content"])[:120]
        out.append(pub)
    return out
