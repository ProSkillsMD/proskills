"""Adapter: awesome-list README parsers (links to github repos / skill folders)."""
from __future__ import annotations

from typing import Any, Callable

from .base import github_links, observation, parse_github_link

NAME = "awesome"


def parse_readme(text: str, list_repo: str) -> list[tuple[str, str, str | None, str]]:
    """(owner, repo, subpath, url) for every github repo link in an awesome-list README.

    Links back to the list itself are dropped; duplicates (same owner/repo/subpath) collapse.
    """
    out, seen = [], set()
    self_key = list_repo.lower()
    for url in github_links(text):
        parsed = parse_github_link(url)
        if not parsed:
            continue
        owner, repo, sub = parsed
        if f"{owner}/{repo}".lower() == self_key:
            continue
        key = (owner.lower(), repo.lower(), (sub or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((owner, repo, sub, url))
    return out


def collect(fetch_text: Callable[[str, str, str], str | None], lists: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """fetch_text(owner, repo, path) -> README text (cached raw fetch, tries default branches)."""
    obs, meta = [], {}
    for entry in lists:
        list_repo = entry["repo"]
        owner, _, repo = list_repo.partition("/")
        text = fetch_text(owner, repo, entry.get("path") or "README.md")
        if text is None:
            meta[list_repo] = {"ok": False, "links": 0}
            continue
        links = parse_readme(text, list_repo)
        meta[list_repo] = {"ok": True, "links": len(links)}
        src = f"{NAME}:{list_repo.lower()}"
        for pos, (o, r, sub, url) in enumerate(links, 1):
            obs.append(observation(o, r, src, f"https://github.com/{list_repo}", subpath=sub,
                                   metrics={"position": pos, "link": url}))
    return obs, meta
