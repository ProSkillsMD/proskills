"""Adapter: GitHub topic search (topic:<t>, sorted by stars; optionally also by recent push).

Stars order returns the same top repos every run, so once those are in the catalog the topic adds
nothing new. ``sorts=("stars", "updated")`` also reads the recently pushed repos of each topic.
"""
from __future__ import annotations

from typing import Any

from .base import RepoSearch, observation

NAME = "github_topic"


def collect(search: RepoSearch, topics: list[str], max_pages: int = 3,
            sorts: tuple[str, ...] | list[str] = ("stars",), updated_max_pages: int | None = None) -> list[dict[str, Any]]:
    obs = []
    for t in topics:
        for srt in sorts:
            pages = max_pages if srt == "stars" or updated_max_pages is None else updated_max_pages
            obs += _collect_one(search, t, srt, pages)
    return obs


def _collect_one(search: RepoSearch, t: str, sort: str, max_pages: int) -> list[dict[str, Any]]:
    obs = []
    if max_pages <= 0:
        return obs
    for rank, it in enumerate(search.search(f"topic:{t}", sort=sort, max_pages=max_pages), 1):
        owner, _, repo = str(it.get("full_name") or "").partition("/")
        if not owner or not repo:
            continue
        obs.append(observation(owner, repo, f"{NAME}:{t}", f"https://github.com/topics/{t}",
                               metrics={"stars": it.get("stars"), "forks": it.get("forks"), "rank": rank},
                               ) | {"_search": it})
    return obs
