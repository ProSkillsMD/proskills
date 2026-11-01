#!/usr/bin/env python3
"""Refresh upstream GitHub metadata on existing catalog rows.

``catalog_update.py`` skips identities that are already listed.
``backfill_skill_files.py`` refreshes ``skill_md`` / ``files`` only.
``clawhub_stats.py`` refreshes ClawHub counts only. The website builder
``scripts/catalog/build_catalog.py`` keeps the previous ``github_stars``,
``last_commit``, and ``readme``. This command re-reads GitHub and writes those
fields back, plus ``open_issues``, ``homepage``, a missing author avatar or
GitHub URL, ``version`` when a real one exists, and ``last_synced_at``.

The reference shape is the ``rederive`` row after website PR #368. ``id``,
``slug``, ``category``, ``reviewed``, and ``scores`` are never changed, and a
row is never deleted. A repository GitHub will not return (deleted, private,
or missing) is recorded in the state file. A rename is recorded the same way;
``repo_url`` stays.

Dry-run is the default and prints a summary. ``--apply`` opens a catalog-only
website PR on ``operator/catalog-publish-*`` (the prefix the existing merge
tooling squash-merges) and does not merge. It refuses to open that PR between
minute :44 and :58 Asia/Dhaka, or while another operator publish PR is open.

No AI usage. Tokens are not printed.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PROJECT_ROOT, emit, exit_fail, exit_ok, redact_secrets  # noqa: E402
from issue_flow import no_handles  # noqa: E402
from publish_lib import catalog_json_dumps, parse_skill_frontmatter  # noqa: E402
from publish_run import CATALOG_REL, DHAKA, HEAD_PREFIX, WEBSITE_REPO, now_dhaka  # noqa: E402
from scout import GitHubError, NAME_OK_RE  # noqa: E402
from skill_files import (  # noqa: E402
    diff_bytes,
    folder_of_skill,
    load_ranking,
    make_client,
    order_fingerprint,
    order_skills,
    owner_repo_of,
)

README_CAP = 8000
DEFAULT_LIMIT = 20
GRAPHQL_BATCH = 20
DEFAULT_STATE = PROJECT_ROOT / "state" / "repo-metadata-refresh.json"
# Heads that block a new batch. Merge tooling only squash-merges the publish prefix.
COLLIDING_PREFIXES = (HEAD_PREFIX, "operator/catalog-refresh-")
PUBLISH_WINDOW = (44, 58)  # inclusive minutes, Asia/Dhaka
PLACEHOLDER_VERSIONS = frozenset({
    "", "0", "0.0", "0.0.0", "0.0.0.0", "v0.0.0", "null", "none", "undefined", "n/a",
})
LOCKED_FIELDS = ("id", "slug", "category", "reviewed", "scores")
STAGE = "repo_metadata_refresh"

Runner = Callable[[Sequence[str], Path | None], "subprocess.CompletedProcess[str]"]


class _Leave:
    """Sentinel: do not write this field."""


LEAVE = _Leave()


def in_publisher_window(now: datetime | None = None) -> bool:
    """True from minute :44 through :58 Asia/Dhaka, when the publisher opens and merges."""
    moment = now or now_dhaka()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=DHAKA)
    minute = moment.astimezone(DHAKA).minute
    start, end = PUBLISH_WINDOW
    return start <= minute <= end


def as_iso(now: datetime | str) -> str:
    if isinstance(now, str):
        return now
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def real_version(value: Any) -> str | None:
    """A version worth writing. Placeholders such as ``0.0.0`` are not real."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    else:
        text = str(value).strip().strip("'\"")
    if not text or len(text) > 64 or any(ch in text for ch in "\r\n\t"):
        return None
    if text.lower() in PLACEHOLDER_VERSIONS:
        return None
    return text


def canonical_avatar(url: str | None) -> str | None:
    """Avatar URL without a size query, matching the rederive row (``?v=4``)."""
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host != "avatars.githubusercontent.com":
        return text
    # ``s`` / ``size`` are display hints and ``u`` is a cache-buster. Keeping them
    # would rewrite the row whenever GitHub rotates the hint. The rederive row is ``?v=4``.
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in ("s", "size", "u")]
    if not any(key == "v" for key, _ in query):
        query.append(("v", "4"))
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", urlencode(query), ""))


def normalize_commit_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedupe_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            continue
        seen.add(path)
        out.append(path)
    return out


def version_paths(folder: str | None) -> list[str]:
    """First real version wins. Skill-folder plugin.json before the repo root, then SKILL.md."""
    paths: list[str] = []
    if folder:
        paths.append(f"{folder}/.claude-plugin/plugin.json")
    paths.append(".claude-plugin/plugin.json")
    if folder:
        paths.append(f"{folder}/plugin.json")
        paths.append(f"{folder}/package.json")
    paths.append("package.json")
    if folder:
        paths.append(f"{folder}/SKILL.md")
    paths.append("SKILL.md")
    return _dedupe_paths(paths)


def readme_paths(folder: str | None) -> list[str]:
    paths: list[str] = []
    if folder:
        paths.append(f"{folder}/README.md")
    paths.append("README.md")
    return _dedupe_paths(paths)


def candidate_paths(folder: str | None) -> list[str]:
    return _dedupe_paths(version_paths(folder) + readme_paths(folder))


def version_from_json(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return real_version(data.get("version"))


def pick_version(folder: str | None, files: dict[str, str | None]) -> str | None:
    for path in version_paths(folder):
        text = files.get(path)
        if not isinstance(text, str):
            continue
        if path.endswith(".md"):
            version = real_version(parse_skill_frontmatter(text).get("version"))
        else:
            version = version_from_json(text)
        if version:
            return version
    return None


def pick_readme(folder: str | None, files: dict[str, str | None]) -> str | None:
    for path in readme_paths(folder):
        text = files.get(path)
        if isinstance(text, str) and text.strip():
            return text.replace("\r\n", "\n").replace("\r", "\n")
    return None


def next_readme(stored_present: bool, stored: Any, upstream: str) -> str | _Leave:
    """Capped README to store, or ``LEAVE`` when upstream matches what is already there."""
    capped = upstream[:README_CAP]
    if stored_present and isinstance(stored, str):
        if stored == capped or stored == upstream:
            return LEAVE
        if len(stored) >= README_CAP and upstream.startswith(stored):
            return LEAVE
    if not capped:
        return LEAVE
    return capped


def _compact(key: str, old: Any, new: Any) -> dict[str, Any]:
    if key == "readme":
        return {
            "from_bytes": len(old) if isinstance(old, str) else 0,
            "to_bytes": len(new) if isinstance(new, str) else 0,
        }
    return {"from": old, "to": new}


def refresh_skill_metadata(skill: dict[str, Any], fetched: dict[str, Any], *, now: str) -> dict[str, Any]:
    """Return ``{status, skill, changes}``. ``unchanged`` keeps the original object.

    ``fetched`` is one repository result: stars, issues, homepage, last_commit,
    author fields, ``files``, and optional ``renamed_to``. Locked fields are copied
    through from ``skill`` and never taken from ``fetched``.
    """
    updated = copy.deepcopy(skill)
    changes: dict[str, Any] = {}

    def set_if(key: str, value: Any, *, only_if_blank: bool = False) -> None:
        if only_if_blank and not _blank(updated.get(key)):
            return
        present = key in updated
        old = updated.get(key)
        if present and old == value:
            return
        if not present and value is None:
            return
        changes[key] = _compact(key, old if present else None, value)
        updated[key] = value

    set_if("github_stars", int(fetched.get("stars") or 0))
    if fetched.get("last_commit"):
        set_if("last_commit", fetched["last_commit"])
    set_if("open_issues", int(fetched.get("open_issues") or 0))
    if fetched.get("homepage"):
        set_if("homepage", fetched["homepage"])
    if fetched.get("author_github"):
        set_if("author_github", fetched["author_github"], only_if_blank=True)
    if fetched.get("author_avatar"):
        set_if("author_avatar", fetched["author_avatar"], only_if_blank=True)

    folder = folder_of_skill(skill)
    files = fetched.get("files") or {}
    version = pick_version(folder, files)
    if version:
        set_if("version", version)
    readme = pick_readme(folder, files)
    if isinstance(readme, str):
        nxt = next_readme("readme" in skill, skill.get("readme"), readme)
        if not isinstance(nxt, _Leave):
            set_if("readme", nxt)

    if changes or _blank(updated.get("last_synced_at")):
        set_if("last_synced_at", now)

    for key in LOCKED_FIELDS:
        if key in skill:
            updated[key] = copy.deepcopy(skill[key])
        elif key in updated and key not in skill:
            updated.pop(key, None)

    if not changes:
        return {"status": "unchanged", "skill": skill, "changes": {}, "renamed_to": fetched.get("renamed_to")}
    return {"status": "updated", "skill": updated, "changes": changes, "renamed_to": fetched.get("renamed_to")}


def github_rows(skills: list[dict[str, Any]], ranking: dict[str, float]) -> list[dict[str, Any]]:
    """GitHub-backed skills, highest traffic first. Non-GitHub rows are omitted."""
    backed = [skill for skill in skills if isinstance(skill, dict) and owner_repo_of(skill)]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in order_skills(backed, ranking):
        slug = row["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        rows.append(row)
    return rows


def repo_specs_for(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One spec per repository, in first-seen order, with the union of file paths."""
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for skill in skills:
        located = owner_repo_of(skill)
        if not located:
            continue
        owner, repo = located
        key = f"{owner}/{repo}"
        spec = grouped.get(key)
        if spec is None:
            spec = {"key": key, "owner": owner, "repo": repo, "paths": []}
            grouped[key] = spec
            order.append(key)
        for path in candidate_paths(folder_of_skill(skill)):
            if path not in spec["paths"]:
                spec["paths"].append(path)
    return [grouped[key] for key in order]


def build_refresh_query(specs: list[dict[str, Any]]) -> str:
    """One aliased GraphQL selection per repository, including the file blobs it needs."""
    blocks: list[str] = []
    for index, spec in enumerate(specs):
        files = []
        for file_index, path in enumerate(spec.get("paths") or []):
            expr = json.dumps("HEAD:" + path)
            files.append(
                f"f{file_index}: object(expression: {expr}) {{ ... on Blob {{ text byteSize }} }}"
            )
        file_ql = (" " + " ".join(files)) if files else ""
        blocks.append(
            "r{index}: repository(owner: {owner}, name: {name}) "
            "{{ nameWithOwner stargazerCount homepageUrl "
            "issues(states: [OPEN]) {{ totalCount }} "
            "defaultBranchRef {{ name target {{ ... on Commit {{ committedDate }} }} }} "
            "owner {{ login avatarUrl }}{files} }}"
            .format(
                index=index,
                owner=json.dumps(spec["owner"]),
                name=json.dumps(spec["repo"]),
                files=file_ql,
            )
        )
    return "query { " + " ".join(blocks) + " rateLimit { remaining resetAt cost } }"


def interpret_node(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    owner = node.get("owner") if isinstance(node.get("owner"), dict) else {}
    login = str(owner.get("login") or "").strip()
    full = str(node.get("nameWithOwner") or "").strip()
    target = ((node.get("defaultBranchRef") or {}).get("target") or {}) if isinstance(node.get("defaultBranchRef"), dict) else {}
    homepage = node.get("homepageUrl")
    if isinstance(homepage, str):
        homepage = homepage.strip() or None
    else:
        homepage = None
    files: dict[str, str | None] = {}
    for index, path in enumerate(spec.get("paths") or []):
        blob = node.get(f"f{index}")
        if isinstance(blob, dict) and isinstance(blob.get("text"), str):
            files[path] = blob["text"]
        else:
            files[path] = None
    renamed = full if full and full.lower() != spec["key"] else None
    return {
        "stars": int(node.get("stargazerCount") or 0),
        "open_issues": int(((node.get("issues") or {}).get("totalCount")) or 0),
        "homepage": homepage,
        "last_commit": normalize_commit_time(target.get("committedDate") if isinstance(target, dict) else None),
        "author_github": f"https://github.com/{login}" if login and NAME_OK_RE.match(login) else None,
        "author_avatar": canonical_avatar(owner.get("avatarUrl") if isinstance(owner.get("avatarUrl"), str) else None),
        "name_with_owner": full or None,
        "renamed_to": renamed,
        "files": files,
    }


def parse_refresh_payload(specs: list[dict[str, Any]], payload: dict[str, Any] | None) -> dict[str, Any]:
    """Map each spec key to a metadata dict, ``not_found``, or ``transient``."""
    data = (payload or {}).get("data") or {}
    if not isinstance(data, dict):
        data = {}
    err_by: dict[str, list[dict[str, Any]]] = {}
    for err in (payload or {}).get("errors") or []:
        path = (err or {}).get("path") or []
        if path:
            err_by.setdefault(str(path[0]), []).append(err)
    out: dict[str, Any] = {}
    for index, spec in enumerate(specs):
        alias = f"r{index}"
        node = data.get(alias)
        errs = err_by.get(alias) or []
        if isinstance(node, dict):
            out[spec["key"]] = interpret_node(spec, node)
            continue
        if any((err.get("type") == "NOT_FOUND") for err in errs) or not errs:
            out[spec["key"]] = "not_found"
        else:
            out[spec["key"]] = "transient"
    return out


def _valid_spec(spec: dict[str, Any]) -> bool:
    owner, repo = spec.get("owner") or "", spec.get("repo") or ""
    return bool(NAME_OK_RE.match(owner) and NAME_OK_RE.match(repo) and repo not in (".", ".."))


def fetch_refresh(client: Any, specs: list[dict[str, Any]], *, batch: int = GRAPHQL_BATCH) -> dict[str, Any]:
    """Batched GraphQL. A failed multi-repo chunk is retried one repository at a time."""
    out: dict[str, Any] = {}
    valid: list[dict[str, Any]] = []
    for spec in specs:
        if _valid_spec(spec):
            valid.append(spec)
        else:
            out[spec["key"]] = "not_found"
    step = max(1, int(batch))
    for start in range(0, len(valid), step):
        chunk = valid[start:start + step]
        parsed = _fetch_chunk(client, chunk)
        if parsed is None and len(chunk) > 1:
            for spec in chunk:
                one = _fetch_chunk(client, [spec])
                out[spec["key"]] = "transient" if one is None else one[spec["key"]]
        elif parsed is None:
            out[chunk[0]["key"]] = "transient"
        else:
            out.update(parsed)
    return out


def _fetch_chunk(client: Any, chunk: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        payload = client.graphql(build_refresh_query(chunk))
    except GitHubError:
        return None
    if not isinstance(payload, dict):
        return None
    return parse_refresh_payload(chunk, payload)


class RefreshState:
    """Resumable cursor. A finished pass starts clean; unavailable repos stay recorded."""

    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = self._empty("")

    def _empty(self, fingerprint: str) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "order_fingerprint": fingerprint,
            "finished": False,
            "completed": {},
            "unavailable": {},
            "unavailable_log": [],
            "renames": {},
            "errors": {},
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

    def _same_pass(self, order: list[str]) -> bool:
        return self.data.get("order_fingerprint") == order_fingerprint(order) and not self.data.get("finished")

    def begin(self, order: list[str]) -> None:
        """Start or resume this order. A new order keeps rename notes and the unavailable log."""
        if self._same_pass(order):
            return
        renames = dict(self.data.get("renames") or {})
        log = list(self.data.get("unavailable_log") or [])
        self.data = self._empty(order_fingerprint(order))
        self.data["renames"] = renames
        self.data["unavailable_log"] = log

    def pending(self, order: list[str]) -> list[str]:
        if not self._same_pass(order):
            return list(order)
        done = self.data.get("completed") or {}
        return [slug for slug in order if slug not in done]

    def complete(self, slug: str, status: str, at: str) -> None:
        self.data.setdefault("completed", {})[slug] = {"status": status, "at": at}

    def note_unavailable(self, key: str, slugs: list[str], at: str) -> None:
        record = {"reason": "deleted_private_or_missing", "at": at, "slugs": list(slugs)}
        self.data.setdefault("unavailable", {})[key] = record
        log = self.data.setdefault("unavailable_log", [])
        log.append({"repo": key, **record})
        if len(log) > 500:
            del log[:-500]

    def clear_unavailable(self, key: str) -> None:
        (self.data.get("unavailable") or {}).pop(key, None)

    def note_rename(self, old: str, new: str) -> None:
        if old and new and old.lower() != new.lower():
            self.data.setdefault("renames", {})[old] = new

    def note_error(self, key: str, at: str) -> None:
        self.data.setdefault("errors", {})[key] = {"reason": "transient", "at": at}

    def clear_error(self, key: str) -> None:
        (self.data.get("errors") or {}).pop(key, None)

    def finish_if_done(self, order: list[str]) -> None:
        done = self.data.get("completed") or {}
        if order and all(slug in done for slug in order):
            self.data["finished"] = True


def _index(skills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        slug = str(skill.get("slug") or skill.get("id") or "").strip()
        if slug and slug not in out:
            out[slug] = skill
    return out


def run_refresh(
    catalog: dict[str, Any],
    ranking: dict[str, float],
    *,
    state: RefreshState | None,
    apply: bool,
    limit: int,
    now: str,
    slugs: list[str] | None = None,
    client: Any = None,
    fetch: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    batch: int = GRAPHQL_BATCH,
) -> dict[str, Any]:
    """Inspect up to ``limit`` pending GitHub rows. Does not write the state file."""
    skills = [skill for skill in (catalog.get("skills") or []) if isinstance(skill, dict)]
    rows = github_rows(skills, ranking)
    order = [row["slug"] for row in rows]
    by_slug = _index(skills)
    if state is not None:
        if apply:
            state.begin(order)
        pending = state.pending(order)
    else:
        pending = list(order)
    allow = {str(slug) for slug in (slugs or [])}
    if allow:
        pending = [slug for slug in pending if slug in allow]
    window = pending[: max(0, int(limit))]
    selected = [by_slug[slug] for slug in window if slug in by_slug]

    fetcher = fetch
    if fetcher is None:
        def fetcher(specs: list[dict[str, Any]], _client: Any = client, _batch: int = batch) -> dict[str, Any]:
            github = _client if _client is not None else make_client()
            return fetch_refresh(github, specs, batch=_batch)

    specs = repo_specs_for(selected)
    fetched = fetcher(specs) if specs else {}
    if not isinstance(fetched, dict):
        fetched = {}

    replaced: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    unchanged: list[str] = []
    unavailable: list[dict[str, Any]] = []
    renames: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    changes: dict[str, Any] = {}
    inspected: list[dict[str, str]] = []
    missing_slugs: dict[str, list[str]] = {}
    seen_renames: set[str] = set()
    known_errors = set((state.data.get("errors") or {})) if (state is not None and apply) else set()

    for skill in selected:
        located = owner_repo_of(skill)
        slug = str(skill.get("slug") or skill.get("id") or "")
        if not located:
            continue
        key = f"{located[0]}/{located[1]}"
        result = fetched.get(key, "transient")
        if result == "not_found":
            unavailable.append({"repo": key, "slug": slug, "reason": "deleted_private_or_missing"})
            inspected.append({"slug": slug, "status": "unavailable"})
            missing_slugs.setdefault(key, []).append(slug)
            continue
        if not isinstance(result, dict):
            # The first failure stays pending so the next run retries it. A failure already
            # recorded before this run is completed as ``error`` so one dead repository
            # cannot occupy the front of every later batch. The catalog row is not changed.
            errors.append({"repo": key, "slug": slug, "reason": "transient"})
            if state is not None and apply:
                state.note_error(key, now)
            inspected.append({"slug": slug, "status": "error" if key in known_errors else "transient"})
            continue
        if state is not None and apply:
            state.clear_error(key)
            state.clear_unavailable(key)
            if result.get("renamed_to"):
                state.note_rename(key, str(result["renamed_to"]))
        if result.get("renamed_to") and key not in seen_renames:
            seen_renames.add(key)
            renames.append({"from": key, "to": str(result["renamed_to"])})
        outcome = refresh_skill_metadata(skill, result, now=now)
        status = outcome["status"]
        inspected.append({"slug": slug, "status": status})
        if status == "updated":
            replaced[slug] = outcome["skill"]
            updated.append(slug)
            changes[slug] = outcome["changes"]
        else:
            unchanged.append(slug)

    if state is not None and apply:
        for key, slugs_for_repo in missing_slugs.items():
            state.note_unavailable(key, slugs_for_repo, now)
            state.clear_error(key)

    new_skills = []
    for skill in skills:
        slug = str(skill.get("slug") or skill.get("id") or "")
        new_skills.append(replaced.get(slug, skill))
    new_catalog = dict(catalog)
    new_catalog["skills"] = new_skills
    stopped = "transient" if errors and not updated and not unchanged and not unavailable else None
    return {
        "catalog": new_catalog,
        "order": rows,
        "order_total": len(order),
        "pending": len(pending),
        "planned": window,
        "updated": updated,
        "unchanged": unchanged,
        "unavailable": unavailable,
        "renames": renames,
        "errors": errors,
        "changes": changes,
        "inspected": inspected,
        "diff_bytes": diff_bytes(catalog, new_catalog) if updated else 0,
        "stopped": stopped,
    }


def catalog_pr_title(stamp: str, updated: int, total: int) -> str:
    return f"operator: catalog metadata refresh {stamp} ({updated} rows, total {total})"


def catalog_pr_body(slugs: list[str], changes: dict[str, Any], *, old_total: int, new_total: int, diff_bytes_n: int) -> str:
    """Body the publisher already understands. ``added_count`` is 0: nothing new was listed."""
    stats = {
        "old_total": old_total,
        "new_total": new_total,
        "added_count": 0,
        "removed_count": 0,
        "refreshed_count": len(slugs),
        "updated_slugs_sample": slugs[:20],
        "diff_bytes": diff_bytes_n,
    }
    summary = {slug: changes.get(slug) or {} for slug in slugs[:20]}
    body = (
        "## Summary\n"
        "- Refresh upstream GitHub metadata on existing catalog rows "
        "(stars, last commit, open issues, homepage, missing author avatar/GitHub, "
        "real version, README when it changed, last_synced_at).\n"
        "- id, slug, category, reviewed, and scores are unchanged. No row is removed.\n"
        f"- Diff stats: {json.dumps(stats, sort_keys=True)}\n"
        f"- Field changes: {json.dumps(summary, sort_keys=True)}\n\n"
        "<!-- proskills:publish v1 items=[] -->\n"
        "<!-- proskills:metadata-refresh v1 slugs="
        + json.dumps(slugs, separators=(",", ":")).replace("@", "\\u0040")
        + " -->\n"
    )
    if not no_handles(body):
        body = body.replace("@", "\\u0040")
    return body


def _run(cmd: Sequence[str], cwd: Path | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False, timeout=180)


def push_catalog_pr(
    run: Runner,
    website: Path,
    catalog: dict[str, Any],
    *,
    branch: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Commit only the catalog file and open a PR. Does not merge.

    The git steps match ``backfill_skill_files.push_catalog_pr``. The branch has to
    start with ``operator/catalog-publish-`` so ``merge_stuck_publish_pr.py`` can see it.
    """
    rel = CATALOG_REL
    status = run(["git", "status", "--porcelain"], website)
    if (status.stdout or "").strip():
        return {"ok": False, "reason": "dirty"}
    if not branch.startswith(HEAD_PREFIX):
        return {"ok": False, "reason": "bad_branch"}
    checked = run(["git", "checkout", "-q", "-b", branch], website)
    if checked.returncode != 0:
        return {"ok": False, "reason": "checkout_failed"}
    target = website / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(catalog_json_dumps(catalog) + "\n", encoding="utf-8")
    run(["git", "add", rel], website)
    diff = run(["git", "diff", "--cached", "--name-only"], website)
    names = [line.strip() for line in (diff.stdout or "").splitlines() if line.strip()]
    if names != [rel]:
        return {"ok": False, "reason": "not_catalog_only", "files": names}
    committed = run(["git", "commit", "-q", "-m", title], website)
    if committed.returncode != 0:
        return {"ok": False, "reason": "commit_failed"}
    pushed = run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], website)
    if pushed.returncode != 0:
        return {"ok": False, "reason": "push_failed", "stderr": redact_secrets(pushed.stderr or "")[-300:]}
    created = run(
        ["gh", "pr", "create", "-R", WEBSITE_REPO, "--base", "main", "--head", branch, "--title", title, "--body", body],
        website,
    )
    return {
        "ok": created.returncode == 0,
        "files": names,
        "branch": branch,
        "merge_invoked": False,
        "stdout": redact_secrets(created.stdout or "")[-400:],
        "stderr": redact_secrets(created.stderr or "")[-400:],
    }


def _gh_json(run: Runner, args: list[str]) -> Any:
    proc = run(["gh", *args], None)
    if proc.returncode != 0:
        detail = redact_secrets((proc.stderr or proc.stdout or "gh failed"))[:300]
        raise RuntimeError(detail)
    return json.loads(proc.stdout or "null")


def open_publish_heads(prs: list[dict[str, Any]]) -> list[str]:
    heads = []
    for pr in prs:
        head = str(pr.get("headRefName") or "")
        if head.startswith(COLLIDING_PREFIXES):
            heads.append(head)
    return heads


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh upstream GitHub metadata on existing catalog rows.")
    parser.add_argument("--catalog", type=Path, required=True, help="skills-catalog.json to read")
    parser.add_argument("--ranking", type=Path, default=None, help="CSV slug,views or JSON ranking")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="resume file under operator/state")
    parser.add_argument("--apply", action="store_true", help="refresh rows and open a catalog PR")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="rows inspected per run")
    parser.add_argument("--slug", action="append", default=[], help="restrict to this slug (repeatable)")
    parser.add_argument("--website", type=Path, default=None, help="website checkout, required with --apply")
    parser.add_argument("--stamp", default=None, help="branch timestamp override (tests)")
    parser.add_argument("--batch", type=int, default=GRAPHQL_BATCH, help="repositories per GraphQL query")
    return parser.parse_args(argv)


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_total": result["order_total"],
        "pending": result["pending"],
        "planned": result["planned"],
        "updated": result["updated"],
        "unchanged": result["unchanged"],
        "unavailable": result["unavailable"],
        "renames": result["renames"],
        "errors": result["errors"],
        "changes": result["changes"],
        "diff_bytes": result["diff_bytes"],
        "stopped": result["stopped"],
    }


def main(
    argv: list[str] | None = None,
    *,
    run: Runner = _run,
    client: Any = None,
    fetch: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> int:
    args = _parse_args(argv)
    dry_run = not args.apply
    moment = now or now_dhaka()
    stamped = as_iso(moment)
    if args.limit < 1:
        emit(stage=STAGE, status="error", dry_run=dry_run, message="--limit must be >= 1")
        exit_fail(1)
    if args.batch < 1:
        emit(stage=STAGE, status="error", dry_run=dry_run, message="--batch must be >= 1")
        exit_fail(1)
    if args.apply and args.website is None:
        emit(stage=STAGE, status="error", dry_run=False, message="--apply requires --website")
        exit_fail(1)
    if not args.catalog.is_file():
        emit(stage=STAGE, status="error", dry_run=dry_run, message=f"missing catalog: {args.catalog}")
        exit_fail(1)
    if args.apply and in_publisher_window(moment):
        emit(
            stage=STAGE, status="ok", dry_run=False,
            message="WINDOW_SKIP: inside the publisher window (Asia/Dhaka :44-:58); not opening a PR",
        )
        exit_ok()
    if args.apply:
        try:
            opened = _gh_json(run, [
                "pr", "list", "-R", WEBSITE_REPO, "--state", "open", "--limit", "100",
                "--json", "number,headRefName",
            ])
        except RuntimeError as exc:
            emit(stage=STAGE, status="error", dry_run=False, message=f"could not list open PRs: {exc}")
            exit_fail(1)
        heads = open_publish_heads(opened or [])
        if heads:
            emit(
                stage=STAGE, status="ok", dry_run=False,
                message="open publish PR(s) pending; not starting another batch",
                open_prs=heads,
            )
            exit_ok()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    ranking = load_ranking(args.ranking) if args.ranking else {}
    state = RefreshState(args.state)
    state.load()
    result = run_refresh(
        catalog, ranking, state=state, apply=args.apply, limit=args.limit, now=stamped,
        slugs=list(args.slug or []), client=client, fetch=fetch, batch=args.batch,
    )
    pr: dict[str, Any] | None = None
    if args.apply and result["updated"]:
        stamp = args.stamp or moment.astimezone(DHAKA).strftime("%Y%m%d-%H%M%S")
        branch = f"{HEAD_PREFIX}{stamp}"
        old_total = len(catalog.get("skills") or [])
        new_total = len(result["catalog"].get("skills") or [])
        title = catalog_pr_title(stamp, len(result["updated"]), new_total)
        body = catalog_pr_body(
            result["updated"], result["changes"], old_total=old_total, new_total=new_total,
            diff_bytes_n=result["diff_bytes"],
        )
        pr = push_catalog_pr(run, args.website, result["catalog"], branch=branch, title=title, body=body)
        if not pr.get("ok"):
            emit(
                stage=STAGE, status="error", dry_run=False,
                message=f"catalog PR was not opened: {pr.get('reason')}",
                pr=pr, **_summary(result),
            )
            exit_fail(1)
    elif args.apply:
        pr = {"ok": True, "opened": False}

    if args.apply:
        for item in result["inspected"]:
            if item["status"] == "transient":
                continue
            state.complete(item["slug"], item["status"], stamped)
        state.finish_if_done([row["slug"] for row in result["order"]])
        state.save()

    noun = "would update" if dry_run else "updated"
    emit(
        stage=STAGE,
        status="ok",
        dry_run=dry_run,
        message=f"{noun} {len(result['updated'])} skills; diff_bytes={result['diff_bytes']}",
        files_touched=[CATALOG_REL] if pr and pr.get("ok") and pr.get("opened", True) and result["updated"] else [],
        pr=pr,
        **_summary(result),
    )
    exit_ok()
    return 0


if __name__ == "__main__":
    sys.exit(main())
