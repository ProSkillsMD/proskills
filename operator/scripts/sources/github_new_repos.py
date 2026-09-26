"""Adapter: repositories created in the last N days that look like SKILL.md skills.

Repository search cannot filter on file presence, so the query narrows by README/topic/name and the
evaluator confirms a SKILL.md via the git tree before anything becomes a candidate.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .base import RepoSearch, observation, utc_now

NAME = "github_new_repos"


def collect(search: RepoSearch, queries: list[str], created_within_days: int = 60, max_pages: int = 2,
            now=None) -> list[dict[str, Any]]:
    since = ((now or utc_now()) - timedelta(days=created_within_days)).date().isoformat()
    obs = []
    for q in queries:
        full = f"{q} created:>={since} fork:false"
        for rank, it in enumerate(search.search(full, sort="stars", max_pages=max_pages), 1):
            owner, _, repo = str(it.get("full_name") or "").partition("/")
            if not owner or not repo:
                continue
            obs.append(observation(owner, repo, NAME, "https://github.com/search?type=repositories&q="
                                   + full.replace(" ", "+"),
                                   metrics={"stars": it.get("stars"), "created_at": it.get("created_at"),
                                            "rank": rank, "query": q}) | {"_search": it})
    return obs
