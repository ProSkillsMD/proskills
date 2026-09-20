#!/usr/bin/env python3
"""Discover and dedupe publish candidates from GitHub issues (and optional sources).

Dry-run by default. --apply writes cache + output JSON only (no GitHub mutations).
No AI usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PROJECT_ROOT, add_dry_run_apply_flags, emit, exit_fail, exit_ok, resolve_dry_run
from publish_lib import (
    PROTECTED_ISSUES,
    iso_now,
    build_catalog_identity_set,
    content_hash,
    extract_github_urls,
    gh_api_json,
    gh_api_search_issues,
    http_get_json,
    identity_in_catalog,
    issue_is_blocked,
    load_catalog,
    parse_github_source,
)

DEFAULT_CACHE = PROJECT_ROOT / "state" / "discovery-cache"
DEFAULT_OUTPUT = PROJECT_ROOT / "state" / "artifacts" / "discover-candidates.json"
DEFAULT_REPO = "ProSkillsMD/proskills"
DEFAULT_ISSUE_QUERY = f'repo:{DEFAULT_REPO} is:issue is:open label:submission'


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and dedupe publish candidates (Scout). Dry-run by default."
    )
    add_dry_run_apply_flags(parser)
    parser.add_argument("--limit", type=int, default=25, help="Max candidates to emit (default 25)")
    parser.add_argument(
        "--from-issues",
        action="store_true",
        default=False,
        help="Search open submission issues via gh (default source when no other source flags)",
    )
    parser.add_argument(
        "--github-search",
        type=str,
        default=None,
        help="Optional extra GitHub search query (issues or code context; best-effort)",
    )
    parser.add_argument(
        "--clawhub",
        action="store_true",
        default=False,
        help="Best-effort public HTTP discovery from ClawHub (optional; may no-op)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help="Discovery cache directory under operator/state",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Candidates JSON output path",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Local skills-catalog.json (else fetch live URL)",
    )
    parser.add_argument(
        "--issues-json",
        type=Path,
        default=None,
        help="Optional local issues dump (skip gh) for offline/tests",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=DEFAULT_REPO,
        help=f"GitHub repo for issue search (default {DEFAULT_REPO})",
    )
    return parser.parse_args()


def _load_issues_from_gh(repo: str, limit: int, extra_query: str | None) -> list[dict[str, Any]]:
    q = f"repo:{repo} is:issue is:open"
    # Prefer submission-labeled when present; fall back to all open
    try:
        items = gh_api_search_issues(f"{q} label:submission", limit=limit)
    except RuntimeError:
        items = []
    if not items:
        items = gh_api_search_issues(q, limit=limit)
    if extra_query:
        try:
            more = gh_api_search_issues(extra_query, limit=limit)
            seen = {i.get("id") or i.get("number") for i in items}
            for it in more:
                key = it.get("id") or it.get("number")
                if key not in seen:
                    items.append(it)
                    seen.add(key)
        except RuntimeError as exc:
            print(f"warning: --github-search failed: {exc}", file=sys.stderr)
    return items[: max(limit * 3, limit)]  # over-fetch then filter


def _load_issues_local(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("items") or data.get("issues") or [])
    return []


def _issue_fields(issue: dict[str, Any]) -> tuple[int, str, str, str]:
    number = int(issue.get("number") or 0)
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    updated = str(issue.get("updated_at") or issue.get("updatedAt") or "")
    return number, title, body, updated


def _cache_key_for_issue(number: int, updated: str, body: str) -> str:
    digest = content_hash(updated, body)
    return f"issue-{number}-{digest}"


def _maybe_repo_sha(owner: str, repo: str) -> str | None:
    """Best-effort default branch commit SHA via gh api."""
    try:
        data = gh_api_json(f"repos/{owner}/{repo}")
        default = (data or {}).get("default_branch") or "main"
        ref = gh_api_json(f"repos/{owner}/{repo}/commits/{default}")
        return (ref or {}).get("sha")
    except Exception:
        return None


def _stars(owner: str, repo: str) -> int | None:
    try:
        data = gh_api_json(f"repos/{owner}/{repo}")
        return int((data or {}).get("stargazers_count") or 0)
    except Exception:
        return None


def discover_from_issues(
    issues: list[dict[str, Any]],
    catalog_identities: set[str],
    *,
    limit: int,
    fetch_meta: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {
        "seen": 0,
        "protected": 0,
        "blocked_label": 0,
        "no_github": 0,
        "already_in_catalog": 0,
        "emitted": 0,
    }
    candidates: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    for issue in issues:
        if len(candidates) >= limit:
            break
        stats["seen"] += 1
        number, title, body, updated = _issue_fields(issue)
        blocked, reason = issue_is_blocked(issue)
        if blocked:
            if reason == "protected_issue":
                stats["protected"] += 1
            else:
                stats["blocked_label"] += 1
            continue

        urls = extract_github_urls(title, body)
        parsed_list = []
        for u in urls:
            p = parse_github_source(u)
            if p:
                parsed_list.append(p)
        if not parsed_list:
            stats["no_github"] += 1
            continue

        # Prefer first distinct identity; if competing, take first only (discover is scout)
        primary = parsed_list[0]
        identity = primary["identity"]
        if identity in seen_identities:
            continue
        if identity_in_catalog(identity, catalog_identities):
            stats["already_in_catalog"] += 1
            continue

        cache_key = _cache_key_for_issue(number, updated, body)
        stars = None
        if fetch_meta:
            sha = _maybe_repo_sha(primary["owner"], primary["repo"])
            if sha:
                cache_key = f"repo-{primary['owner']}-{primary['repo']}-{sha[:12]}"
            stars = _stars(primary["owner"], primary["repo"])

        cand = {
            "issue": number,
            "identity": identity,
            "repo_url": primary["repo_url"],
            "subpath": primary["subpath"],
            "title": title,
            "stars": stars,
            "source": "github-issue",
            "cache_key": cache_key,
            "owner": primary["owner"],
            "repo": primary["repo"],
        }
        candidates.append(cand)
        seen_identities.add(identity)
        stats["emitted"] += 1

    return candidates, stats


def discover_clawhub(limit: int, catalog_identities: set[str]) -> list[dict[str, Any]]:
    """Best-effort public HTTP; returns [] on failure."""
    # ClawHub public listing is best-effort; several endpoints may 404.
    urls = [
        "https://clawhub.ai/api/skills",
        "https://www.clawhub.ai/api/skills",
    ]
    out: list[dict[str, Any]] = []
    for url in urls:
        data = http_get_json(url)
        if not data:
            continue
        items = data if isinstance(data, list) else (data.get("skills") or data.get("items") or [])
        for item in items:
            if len(out) >= limit:
                break
            raw = None
            if isinstance(item, dict):
                raw = item.get("repo_url") or item.get("github") or item.get("url")
            parsed = parse_github_source(str(raw) if raw else None)
            if not parsed:
                continue
            if identity_in_catalog(parsed["identity"], catalog_identities):
                continue
            out.append(
                {
                    "issue": None,
                    "identity": parsed["identity"],
                    "repo_url": parsed["repo_url"],
                    "subpath": parsed["subpath"],
                    "title": (item.get("name") if isinstance(item, dict) else None) or parsed["repo"],
                    "stars": None,
                    "source": "clawhub",
                    "cache_key": content_hash(parsed["identity"], str(raw)),
                    "owner": parsed["owner"],
                    "repo": parsed["repo"],
                }
            )
        if out:
            break
    return out


def main() -> None:
    args = _parse_args()
    dry_run = resolve_dry_run(args)

    # Default to --from-issues when no offline dump and clawhub-only not set
    use_issues = args.from_issues or args.issues_json is not None or (
        not args.clawhub and args.github_search is None
    )
    if args.github_search and not args.from_issues and args.issues_json is None:
        use_issues = True

    try:
        catalog = load_catalog(args.catalog) if args.catalog else load_catalog(None)
    except RuntimeError as exc:
        # Fall back to sibling website-ops catalog if live fetch fails
        sibling = Path("/workspace/website-ops/public/skills-catalog.json")
        if sibling.is_file():
            catalog = load_catalog(sibling)
            print(f"warning: {exc}; using {sibling}", file=sys.stderr)
        else:
            emit(stage="discover", status="error", dry_run=dry_run, message=str(exc))
            exit_fail(1)

    catalog_identities = build_catalog_identity_set(catalog)
    candidates: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"catalog_size": len(catalog.get("skills") or [])}

    if use_issues:
        if args.issues_json:
            issues = _load_issues_local(args.issues_json)
            fetch_meta = False
        else:
            try:
                issues = _load_issues_from_gh(args.repo, args.limit, args.github_search)
            except RuntimeError as exc:
                emit(stage="discover", status="error", dry_run=dry_run, message=str(exc))
                exit_fail(1)
            fetch_meta = not dry_run  # avoid extra API in dry-run unless apply
        cands, issue_stats = discover_from_issues(
            issues, catalog_identities, limit=args.limit, fetch_meta=fetch_meta
        )
        candidates.extend(cands)
        stats.update(issue_stats)

    if args.clawhub:
        claw = discover_clawhub(args.limit, catalog_identities)
        existing = {c["identity"] for c in candidates}
        for c in claw:
            if c["identity"] not in existing and len(candidates) < args.limit:
                candidates.append(c)
        stats["clawhub_added"] = len(claw)

    payload = {
        "generated_at": iso_now(),
        "dry_run": dry_run,
        "total": len(candidates),
        "candidates": candidates,
        "stats": stats,
        "protected_issues": sorted(PROTECTED_ISSUES),
    }

    plan = {
        "would_write_output": str(args.output),
        "would_write_cache_dir": str(args.cache_dir),
        "candidate_count": len(candidates),
    }

    if dry_run:
        emit(
            stage="discover",
            status="ok",
            dry_run=True,
            message=f"discover dry-run: {len(candidates)} candidates (no writes)",
            plan=plan,
            stats=stats,
            sample=candidates[:3],
        )
        exit_ok()

    # --apply: write cache + output only
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for c in candidates:
        cache_path = args.cache_dir / f"{c['cache_key']}.json"
        cache_path.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(
        stage="discover",
        status="ok",
        dry_run=False,
        message=f"discover wrote {len(candidates)} candidates to {args.output}",
        output=str(args.output),
        cache_dir=str(args.cache_dir),
        stats=stats,
    )
    exit_ok()


if __name__ == "__main__":
    main()
