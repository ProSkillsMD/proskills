"""Adapter: GitHub topic search (topic:<t>, sorted by stars)."""
from __future__ import annotations

from typing import Any

from .base import RepoSearch, observation

NAME = "github_topic"


def collect(search: RepoSearch, topics: list[str], max_pages: int = 3) -> list[dict[str, Any]]:
    obs = []
    for t in topics:
        for rank, it in enumerate(search.search(f"topic:{t}", sort="stars", max_pages=max_pages), 1):
            owner, _, repo = str(it.get("full_name") or "").partition("/")
            if not owner or not repo:
                continue
            obs.append(observation(owner, repo, f"{NAME}:{t}", f"https://github.com/topics/{t}",
                                   metrics={"stars": it.get("stars"), "forks": it.get("forks"), "rank": rank},
                                   ) | {"_search": it})
    return obs
