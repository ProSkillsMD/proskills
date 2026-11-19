#!/usr/bin/env python3
"""Per-skill inline file bytes, kept out of ``public/skills-catalog.json``.

The catalog file is one Git blob. GitHub rejects blobs over 100 MiB
(104,857,600 bytes; the live file has already reached 104,857,042). Inline
``files[].content`` is almost all of that weight. Writers in this repo store
those bytes in one JSON file per skill and leave an index on the catalog row.

No open ProSkillsMD/website pull request defines this split (checked
2026-11-19: open website PRs are unrelated skill listings, and ``main`` still
inlines ``files[].content``. Build time copies heavy fields into gitignored
``public/skill-details/<id>.json`` and does not read ``public/skill-files/``).
Path and schema live only in this module so they can be pointed at whatever
that website change settles on.

Published layout, website-relative:

    public/skills-catalog.json
        skills[].files[] = {path, size, sha, truncated?, omitted?}
        ``content`` is never written.

    public/skill-files/<slug>.json
        {
          "schema": "proskills.skill-files/v1",
          "slug": "<slug>",
          "id": "<catalog id>",
          "files": [
            {"path": "<repo-root path>", "size": 123, "sha": "<40 hex>", "content": "<full text>"}
          ]
        }

Only files that actually have inline text are listed in the per-skill
document. Omitted and truncated files stay on the catalog index. Rewriting an
index that already had its text moved out does not delete the per-skill file.
A capture whose files are all omitted or truncated removes that slug's file.
Capture and refresh still hold ``content`` in memory; stripping happens when a
catalog is written into a website checkout.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from publish_lib import catalog_json_dumps

SCHEMA = "proskills.skill-files/v1"
CATALOG_REL = "public/skills-catalog.json"
SKILL_FILES_DIR = "public/skill-files"
CONTENT_KEY = "content"

# Slugs are ``publish_lib.slugify`` output (lowercase, hyphens, up to 80
# chars) but ids copied onto ``slug`` may also use digits, dots, and
# underscores. One path segment, no separators.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
_SKILL_FILE_RE = re.compile(
    r"^public/skill-files/([A-Za-z0-9][A-Za-z0-9._-]{0,180})\.json$"
)


def skill_slug(skill: dict[str, Any]) -> str | None:
    """Slug used as the per-skill filename, or None when it is not a safe path segment."""
    for key in ("slug", "id"):
        value = skill.get(key)
        if isinstance(value, str) and _SLUG_RE.fullmatch(value):
            return value
    return None


def skill_file_rel(slug: str) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"unsafe skill slug for per-skill file: {slug!r}")
    return f"{SKILL_FILES_DIR}/{slug}.json"


def is_skill_file_rel(path: str) -> bool:
    """True for ``public/skill-files/<slug>.json`` and nothing nested or escaped."""
    return _SKILL_FILE_RE.fullmatch(path or "") is not None


def is_catalog_publish_path(path: str) -> bool:
    return path == CATALOG_REL or is_skill_file_rel(path)


def catalog_publish_diff_ok(paths: list[str] | tuple[str, ...] | None) -> bool:
    """Auto-merge allow-list: the catalog and/or per-skill files, nothing else.

    An empty diff is not a publish. A catalog-only diff stays allowed, and so
    does a diff that also (or only) touches ``public/skill-files/<slug>.json``.
    """
    if not paths:
        return False
    return all(is_catalog_publish_path(path) for path in paths)


def index_file_entry(entry: Any) -> Any:
    """Catalog index row. Drops inline text; keeps path, size, sha, and flags."""
    if not isinstance(entry, dict) or CONTENT_KEY not in entry:
        return entry
    return {key: value for key, value in entry.items() if key != CONTENT_KEY}


def index_skill(skill: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``skill`` whose ``files`` array has no ``content`` keys.

    The same object is returned when there is nothing to strip, so callers can
    use identity to see that the row did not change.
    """
    files = skill.get("files")
    if not isinstance(files, list):
        return skill
    if not any(isinstance(entry, dict) and CONTENT_KEY in entry for entry in files):
        return skill
    updated = dict(skill)
    updated["files"] = [index_file_entry(entry) for entry in files]
    return updated


def index_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Catalog whose skill file lists are indexes. Does not mutate ``catalog``."""
    skills = catalog.get("skills")
    if not isinstance(skills, list):
        return catalog
    indexed = [index_skill(skill) if isinstance(skill, dict) else skill for skill in skills]
    if all(new is old for new, old in zip(indexed, skills)) and len(indexed) == len(skills):
        return catalog
    updated = dict(catalog)
    updated["skills"] = indexed
    return updated


def catalog_has_inline_content(catalog: dict[str, Any]) -> bool:
    for skill in catalog.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        for entry in skill.get("files") or []:
            if isinstance(entry, dict) and isinstance(entry.get(CONTENT_KEY), str):
                return True
    return False


def keeps_side_file(skill: dict[str, Any]) -> bool:
    """True when inline text is in this row or already lives in the per-skill file.

    An index entry is ``{path, size, sha}`` with no ``content`` and no
    ``omitted`` / ``truncated`` flag: the bytes were moved aside and must be
    kept. A capture that stored nothing inline is only omitted/truncated
    entries, and its per-skill file is removed.
    """
    files = skill.get("files")
    if not isinstance(files, list):
        return False
    for entry in files:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get(CONTENT_KEY), str):
            return True
        if entry.get("omitted") or entry.get("truncated"):
            continue
        if isinstance(entry.get("path"), str):
            return True
    return False


def content_document(skill: dict[str, Any]) -> dict[str, Any] | None:
    """Per-skill JSON document, or None when the skill has no inline text."""
    slug = skill_slug(skill)
    if slug is None:
        return None
    files: list[dict[str, Any]] = []
    for entry in skill.get("files") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get(CONTENT_KEY), str):
            continue
        item: dict[str, Any] = {}
        if isinstance(entry.get("path"), str):
            item["path"] = entry["path"]
        if "size" in entry:
            item["size"] = entry["size"]
        if isinstance(entry.get("sha"), str):
            item["sha"] = entry["sha"]
        item[CONTENT_KEY] = entry[CONTENT_KEY]
        files.append(item)
    if not files:
        return None
    return {"schema": SCHEMA, "slug": slug, "id": skill.get("id"), "files": files}


def website_root_from_catalog(catalog_path: Path) -> Path | None:
    """Checkout root when ``catalog_path`` is ``<root>/public/skills-catalog.json``."""
    path = Path(catalog_path)
    if path.name == "skills-catalog.json" and path.parent.name == "public":
        return path.parent.parent
    return None


def _contained(website: Path, rel: str) -> Path:
    root = (website / SKILL_FILES_DIR).resolve()
    target = (website / rel).resolve()
    if target.parent != root:
        raise ValueError(f"per-skill path escapes {SKILL_FILES_DIR}: {rel}")
    return target


def _write_if_changed(path: Path, text: str) -> bool:
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def write_website_catalog(website: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    """Write the index catalog and one content file per skill that has inline text.

    Idempotent: a second call with the index catalog rewrites nothing when the
    bytes already match, and does not delete per-skill files whose text was
    already moved out. A capture that has no inline text removes that slug's
    file. Skills whose slug is not a single safe path segment keep their inline
    text in the catalog so the bytes are not dropped.
    """
    skills_out: list[Any] = []
    for skill in catalog.get("skills") or []:
        if not isinstance(skill, dict):
            skills_out.append(skill)
            continue
        slug = skill_slug(skill)
        document = content_document(skill) if slug else None
        if slug and document is None and not keeps_side_file(skill):
            stale = _contained(website, skill_file_rel(slug))
            if stale.is_file():
                stale.unlink()
            skills_out.append(index_skill(skill))
            continue
        if slug and document is not None:
            rel = skill_file_rel(slug)
            _write_if_changed(_contained(website, rel), catalog_json_dumps(document) + "\n")
            skills_out.append(index_skill(skill))
            continue
        skills_out.append(index_skill(skill) if slug else skill)
    indexed = dict(catalog)
    indexed["skills"] = skills_out
    catalog_path = website / CATALOG_REL
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(catalog_path, catalog_json_dumps(indexed) + "\n")
    return indexed


def git_add_argv(website: Path) -> list[str]:
    """``git add -A`` arguments for the catalog and, when present, the per-skill directory.

    ``-A`` stages new files, edits, and deletions under those paths only.
    """
    args = ["add", "-A", "--", CATALOG_REL]
    if (website / SKILL_FILES_DIR).exists():
        args.append(SKILL_FILES_DIR)
    return args
