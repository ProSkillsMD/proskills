#!/usr/bin/env python3
"""Append new skill records to a staged website skills-catalog.json.

NEVER overwrites existing catalog id/slug/repo for an existing listing.
Dry-run by default; --apply writes staged catalog + delta.
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PROJECT_ROOT, add_dry_run_apply_flags, emit, exit_fail, exit_ok, resolve_dry_run
from publish_lib import (
    build_catalog_identity_set,
    build_skill_record,
    fetch_raw_text,
    iso_now,
    load_catalog,
    parse_github_source,
)

DEFAULT_OUT_DIR = PROJECT_ROOT / "state" / "artifacts"
DEFAULT_STAGED = DEFAULT_OUT_DIR / "skills-catalog.staged.json"
DEFAULT_DELTA = DEFAULT_OUT_DIR / "delta.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage catalog append from candidates.")
    add_dry_run_apply_flags(parser)
    parser.add_argument("--candidates", type=Path, required=True, help="Candidates JSON from discover")
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="Existing skills-catalog.json (website public/ or copy)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_STAGED,
        help="Staged catalog output path (skills-catalog.staged.json)",
    )
    parser.add_argument(
        "--delta-out",
        type=Path,
        default=DEFAULT_DELTA,
        help="Delta JSON of new skills only",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Do not fetch SKILL.md/README from network; use empty stubs",
    )
    parser.add_argument(
        "--fixture-skill-md",
        type=Path,
        default=None,
        help="Optional local SKILL.md used for all candidates (tests)",
    )
    parser.add_argument(
        "--fixture-readme",
        type=Path,
        default=None,
        help="Optional local README used for all candidates (tests)",
    )
    return parser.parse_args()


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return list(data.get("candidates") or [])


def _refs(cand: dict[str, Any]) -> tuple[str, ...]:
    """Refs to try for raw fetches: the candidate's default branch first, then main/master."""
    out: list[str] = []
    for r in (cand.get("default_branch"), "main", "master"):
        if isinstance(r, str) and r.strip() and r.strip() not in out:
            out.append(r.strip())
    return tuple(out)


def _skill_folder(cand: dict[str, Any]) -> str | None:
    """Original-case skill folder: from skill_path (scout keeps case) else the (lower-cased) subpath."""
    sp = str(cand.get("skill_path") or "").strip().strip("/")
    if sp and "/" in sp and sp.rsplit("/", 1)[1].lower() == "skill.md":
        return sp.rsplit("/", 1)[0]
    if sp and sp.lower() != "skill.md" and not sp.lower().endswith(".md"):
        return sp  # catalog-style skill_path (a folder)
    sub = cand.get("subpath")
    return str(sub).strip("/") if sub else None


def source_tree_url(owner: str, repo: str, folder: str | None, branch: str | None) -> str:
    """GitHub URL of the skill source: the repo, or the subfolder tree URL for a subfolder skill."""
    base = f"https://github.com/{owner}/{repo}"
    if not folder:
        return base
    return f"{base}/tree/{branch or 'main'}/{folder}"


def _fetch_docs(cand: dict[str, Any], *, offline: bool, fixture_md: Path | None, fixture_readme: Path | None) -> tuple[str | None, str | None]:
    owner = cand.get("owner")
    repo = cand.get("repo")
    if not owner or not repo:
        parsed = parse_github_source(cand.get("repo_url"))
        if parsed:
            owner, repo = parsed["owner"], parsed["repo"]
    folder = _skill_folder(cand)
    refs = _refs(cand)
    if fixture_md is not None:
        skill_md = fixture_md.read_text(encoding="utf-8")
    elif offline:
        skill_md = None
    else:
        skill_path = f"{folder}/SKILL.md" if folder else "SKILL.md"
        skill_md = fetch_raw_text(owner, repo, skill_path, refs) if owner and repo else None

    if fixture_readme is not None:
        readme = fixture_readme.read_text(encoding="utf-8")
    elif offline:
        readme = None
    else:
        readme = None
        if owner and repo:
            # subfolder skill: its own README first, then the repo README
            if folder:
                readme = fetch_raw_text(owner, repo, f"{folder}/README.md", refs)
            if readme is None:
                readme = fetch_raw_text(owner, repo, "README.md", refs)

    return skill_md, readme


def plan_update(
    catalog: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    offline: bool = False,
    fixture_md: Path | None = None,
    fixture_readme: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (staged_catalog, new_skills, skip_log). Preserves all existing skills."""
    existing = list(catalog.get("skills") or [])
    existing_copy = deepcopy(existing)
    identities = build_catalog_identity_set(catalog)
    used_ids = {str(s.get("id")) for s in existing if s.get("id")}
    used_slugs = {str(s.get("slug")) for s in existing if s.get("slug")}

    new_skills: list[dict[str, Any]] = []
    skip_log: list[dict[str, Any]] = []
    added_map: list[dict[str, Any]] = []

    for cand in candidates:
        identity = cand.get("identity")
        if not identity:
            parsed = parse_github_source(cand.get("repo_url"))
            if parsed and cand.get("subpath"):
                identity = f"github:{parsed['owner']}/{parsed['repo']}::{cand['subpath']}"
            elif parsed:
                identity = parsed["identity"]
            else:
                skip_log.append({"candidate": cand, "reason": "no_identity"})
                continue

        if identity.lower() in {x.lower() for x in identities}:
            skip_log.append({"identity": identity, "reason": "already_in_catalog"})
            continue

        # Also skip if repo_url alone matches a root catalog entry and no subpath
        parsed = parse_github_source(cand.get("repo_url")) or parse_github_source(
            f"{cand.get('repo_url')}/tree/main/{cand['subpath']}" if cand.get("subpath") else None
        )
        if not parsed and cand.get("owner") and cand.get("repo"):
            sub = cand.get("subpath")
            raw = f"https://github.com/{cand['owner']}/{cand['repo']}"
            if sub:
                raw = f"{raw}/tree/main/{sub}"
            parsed = parse_github_source(raw)

        if not parsed:
            skip_log.append({"identity": identity, "reason": "unparseable_repo"})
            continue

        skill_md, readme = _fetch_docs(
            {**cand, **parsed},
            offline=offline,
            fixture_md=fixture_md,
            fixture_readme=fixture_readme,
        )
        folder = _skill_folder({**cand, "subpath": parsed.get("subpath") or cand.get("subpath")})
        record = build_skill_record(
            owner=parsed["owner"],
            repo=parsed["repo"],
            repo_url=parsed["repo_url"],
            subpath=folder,
            skill_md=skill_md,
            readme=readme,
            stars=cand.get("stars"),
            existing_ids=used_ids,
            existing_slugs=used_slugs,
        )
        # Guard: never collide with existing id/slug
        if record["id"] in used_ids or record["slug"] in used_slugs:
            skip_log.append({"identity": identity, "reason": "id_collision", "id": record["id"]})
            continue

        if folder:
            # repo_url stays the bare repo (website derives raw/download URLs from it and dedupes one listing
            # per repo); source_url is the subfolder tree URL of this specific skill.
            record["source_url"] = source_tree_url(parsed["owner"], parsed["repo"], folder,
                                                   (_refs(cand) or ("main",))[0])
        new_skills.append(record)
        added_map.append({"identity": identity.lower(), "issue": cand.get("issue"), "id": record["id"],
                          "slug": record["slug"], "category": record.get("category"),
                          "repo_url": record["repo_url"], "source_url": record.get("source_url") or record["repo_url"]})
        used_ids.add(record["id"])
        used_slugs.add(record["slug"])
        identities.add(identity)

    staged = deepcopy(catalog)
    staged["skills"] = existing_copy + new_skills
    staged["total"] = len(staged["skills"])
    staged["generated_at"] = iso_now()
    staged["version"] = catalog.get("version") or "1.1.0"
    # Verify preserve
    for i, old in enumerate(existing):
        if staged["skills"][i].get("id") != old.get("id"):
            raise RuntimeError("invariant violated: existing skill id changed")
        if staged["skills"][i].get("slug") != old.get("slug"):
            raise RuntimeError("invariant violated: existing skill slug changed")
        if staged["skills"][i].get("repo_url") != old.get("repo_url"):
            raise RuntimeError("invariant violated: existing skill repo_url changed")

    plan_update.last_added_map = added_map  # type: ignore[attr-defined]
    return staged, new_skills, skip_log


def added_identity_map(new_skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """identity -> id/slug mapping of the last plan_update call (publisher: map published skills to issues)."""
    return list(getattr(plan_update, "last_added_map", []))


def main() -> None:
    args = _parse_args()
    dry_run = resolve_dry_run(args)

    if not args.candidates.is_file():
        emit(stage="catalog_update", status="error", dry_run=dry_run, message=f"missing candidates: {args.candidates}")
        exit_fail(1)
    if not args.catalog.is_file():
        emit(stage="catalog_update", status="error", dry_run=dry_run, message=f"missing catalog: {args.catalog}")
        exit_fail(1)

    catalog = load_catalog(args.catalog)
    candidates = _load_candidates(args.candidates)

    try:
        staged, new_skills, skip_log = plan_update(
            catalog,
            candidates,
            offline=args.offline,
            fixture_md=args.fixture_skill_md,
            fixture_readme=args.fixture_readme,
        )
    except RuntimeError as exc:
        emit(stage="catalog_update", status="error", dry_run=dry_run, message=str(exc))
        exit_fail(1)

    plan = {
        "existing_count": len(catalog.get("skills") or []),
        "append_count": len(new_skills),
        "skip_count": len(skip_log),
        "new_ids": [s["id"] for s in new_skills],
        "staged_path": str(args.out),
        "delta_path": str(args.delta_out),
    }

    if dry_run:
        emit(
            stage="catalog_update",
            status="ok",
            dry_run=True,
            message=f"catalog_update dry-run: would append {len(new_skills)} skills",
            plan=plan,
            skips=skip_log[:10],
        )
        exit_ok()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.delta_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(staged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    delta = {
        "generated_at": iso_now(),
        "source_catalog": str(args.catalog),
        "added": new_skills,
        # identity/issue -> id/slug for each added skill (map by identity, not by owner/repo guessing)
        "added_map": added_identity_map(new_skills),
        "skipped": skip_log,
    }
    args.delta_out.write_text(json.dumps(delta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(
        stage="catalog_update",
        status="ok",
        dry_run=False,
        message=f"wrote staged catalog ({staged['total']} skills) + delta ({len(new_skills)} new)",
        staged=str(args.out),
        delta=str(args.delta_out),
        plan=plan,
    )
    exit_ok()


if __name__ == "__main__":
    main()
