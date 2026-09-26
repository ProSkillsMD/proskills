"""Adapter: known publisher orgs (config/sources.json `known_orgs`)."""
from __future__ import annotations

from typing import Any

from .base import RepoSearch, observation

NAME = "known_org"


def collect(search: RepoSearch, orgs: list[str], repos: list[str], org_query: str = "skill in:name,description,readme",
            org_max_results: int = 50) -> list[dict[str, Any]]:
    obs = []
    for full in repos:
        owner, _, repo = full.partition("/")
        if owner and repo:
            obs.append(observation(owner, repo, f"{NAME}:{owner.lower()}", f"https://github.com/{full}",
                                   metrics={"listed": True}))
    for org in orgs:
        items = search.search(f"org:{org} {org_query}", sort="stars", max_pages=1,
                              per_page=max(1, min(100, org_max_results)))
        for rank, it in enumerate(items, 1):
            owner, _, repo = str(it.get("full_name") or "").partition("/")
            if owner and repo:
                obs.append(observation(owner, repo, f"{NAME}:{org.lower()}", f"https://github.com/{org}",
                                       metrics={"stars": it.get("stars"), "rank": rank}) | {"_search": it})
    return obs
