"""Turn repo observations into unified candidate records via the scout's checks.

Same rules as operator/scripts/scout.py: tree-based SKILL.md discovery, subpath-level catalog
dedupe against the live catalog, license tiers (pass / license_review / reject), mandatory
static_scan (critical -> hold), protected / critical / publisher-skip holds. Never executes
candidate code. Emits records only; nothing is queued, filed, staged or published.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import scout
from scout import (GitHubClient, GitHubError, NotFound, RateLimitExhausted, ScoutState, Transient,
                   _covers, _norm_sub, build_catalog_index, catalog_match, fetch_repo_tree_verdict,
                   identity_for, license_tier, run_scan_with_client)
from publish_lib import PROTECTED_ISSUES, parse_skill_frontmatter

from .base import iso, parse_iso

CRITICAL_ISSUES = frozenset({2396, 4869})
PUBLISHER_SKIP_ISSUES = frozenset({2833})
META_FIELDS = ("nameWithOwner pushedAt createdAt isArchived isFork isEmpty stargazerCount forkCount "
               "defaultBranchRef { name target { oid } } licenseInfo { spdxId key name }")


def merge_observations(obs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """repo_key -> {owner, repo, obs:[...]} (case-insensitive)."""
    repos: dict[str, dict[str, Any]] = {}
    for o in obs:
        key = f"{o['owner']}/{o['repo']}".lower()
        e = repos.setdefault(key, {"owner": o["owner"], "repo": o["repo"], "obs": []})
        e["obs"].append(o)
    return repos


def fetch_meta(client: GitHubClient, keys: list[str], batch: int = 50) -> dict[str, dict[str, Any] | str]:
    """repo_key -> meta (scout-compatible + created_at, forks, commit_sha) | 'not_found' | 'transient'."""
    out: dict[str, dict[str, Any] | str] = {}
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        parts = []
        for j, k in enumerate(chunk):
            o, _, r = k.partition("/")
            parts.append(f"r{j}: repository(owner: {json.dumps(o)}, name: {json.dumps(r)}) {{ {META_FIELDS} }}")
        try:
            payload = client.graphql("query { " + " ".join(parts) + " }")
        except GitHubError:
            for k in chunk:
                out[k] = "transient"
            continue
        data = payload.get("data") or {}
        errs = {str((e.get("path") or [None])[0]): e.get("type") for e in payload.get("errors") or [] if e}
        for j, k in enumerate(chunk):
            n = data.get(f"r{j}")
            if n:
                ref = n.get("defaultBranchRef") or {}
                lic = n.get("licenseInfo")
                out[k] = {"full_name": n.get("nameWithOwner") or k, "pushed_at": n.get("pushedAt"),
                          "created_at": n.get("createdAt"), "archived": bool(n.get("isArchived")),
                          "fork": bool(n.get("isFork")), "empty": bool(n.get("isEmpty")),
                          "stars": int(n.get("stargazerCount") or 0), "forks": int(n.get("forkCount") or 0),
                          "default_branch": ref.get("name"), "commit_sha": (ref.get("target") or {}).get("oid"),
                          "license": ({"spdx_id": lic.get("spdxId"), "key": lic.get("key"), "name": lic.get("name")}
                                      if lic else None)}
            elif errs.get(f"r{j}") == "NOT_FOUND":
                out[k] = "not_found"
            else:
                out[k] = "transient"
    return out


def load_hold_rules(client: GitHubClient | None, cache: Any, art: Path, issues_ttl_s: float = 86400) -> dict[str, Any]:
    """Identity/repo-level holds: protected, critical_static and publisher-skip issue targets, plus
    identities held in the current/previous candidate queue."""
    rules: dict[str, Any] = {"identities": {}, "repos": {}}
    wanted = [(n, "protected_issue") for n in sorted(PROTECTED_ISSUES)] + \
             [(n, "critical_static_issue") for n in sorted(CRITICAL_ISSUES)] + \
             [(n, "publisher_skip_issue") for n in sorted(PUBLISHER_SKIP_ISSUES)]
    for n, kind in wanted:
        issue = cache.get(f"issue:{n}", issues_ttl_s) if cache is not None else None
        if issue is None and client is not None:
            try:
                raw = client.rest(f"repos/{scout.REPO_SLUG}/issues/{n}") or {}
                issue = {"number": n, "title": raw.get("title") or "", "body": raw.get("body") or ""}
                if cache is not None:
                    cache.put(f"issue:{n}", issue)
            except GitHubError:
                issue = None
        t = scout.issue_target(issue) if issue else None
        if not t:
            continue
        reason = f"{kind}:#{n}"
        if t.subpath:
            rules["identities"][t.identity] = reason
        else:
            rules["repos"][t.repo_key] = reason
    today = datetime.now(scout.DHAKA).date()
    for d in (today, today - timedelta(days=1)):
        q = scout._load_json(art / f"candidate-queue-{d.isoformat()}.json", {})
        for key in ("holds_critical_static", "holds_other_scan"):
            for h in q.get(key) or []:
                if isinstance(h, dict) and h.get("identity"):
                    rules["identities"].setdefault(str(h["identity"]).lower(),
                                                   f"queue_{key}:{h.get('hold') or 'hold'}")
    return rules


def hold_reason(rules: dict[str, Any], identity: str, repo_key: str) -> str | None:
    ident = identity.lower()
    if ident in rules["identities"]:
        return rules["identities"][ident]
    for held, reason in rules["identities"].items():
        if held.startswith("github:") and ident.startswith(held + "/"):
            return reason
    return rules["repos"].get(repo_key.lower())


def existing_issue_index(art: Path) -> tuple[dict[str, int], set[str]]:
    """identity -> issue (from the candidate queue) and repo keys claimed by open issues (scout cache keys)."""
    by_ident: dict[str, int] = {}
    today = datetime.now(scout.DHAKA).date()
    for d in (today - timedelta(days=1), today):
        q = scout._load_json(art / f"candidate-queue-{d.isoformat()}.json", {})
        for key in ("eligible", "passed", "license_review", "holds_critical_static", "holds_other_scan"):
            for x in q.get(key) or []:
                if isinstance(x, dict) and x.get("identity") and x.get("issue"):
                    by_ident[str(x["identity"]).lower()] = int(x["issue"])
    repos = set(scout._load_json(scout.SCOUT_STATE / "repo-cache.json", {}).keys())
    return by_ident, repos


class SourceEvaluator:
    def __init__(self, client: GitHubClient, state: ScoutState, catalog: dict[str, Any], *,
                 hold_rules: dict[str, Any], limits: dict[str, Any], issue_index=None,
                 fallback_repo_cache: dict[str, Any] | None = None, scan: bool = True,
                 workers: int = 6, log: Callable[[str], None] = lambda m: None):
        self.client = client
        self.state = state
        self.catalog_idx = build_catalog_index(catalog)
        self.holds = hold_rules
        self.lim = limits
        self.issue_by_ident, self.issue_repos = issue_index or ({}, set())
        self.fallback = fallback_repo_cache or {}
        self.scan_enabled = scan
        self.workers = workers
        self.log = log
        self.rules_hash = scout._scan_rules_hash()
        self.repo_outcomes: Counter = Counter()
        self.repo_outcome_by_key: dict[str, str] = {}
        self._stop = threading.Event()

    # -- repo level -------------------------------------------------------------
    def _cached_verdict(self, key: str, meta: dict[str, Any]) -> dict[str, Any] | None:
        max_age = 30
        v = self.state.repo_fresh(key, meta.get("pushed_at"), max_age)
        if v:
            return v
        e = self.fallback.get(key)
        if e and meta.get("pushed_at") and e.get("pushed_at") == meta.get("pushed_at"):
            checked = parse_iso(e.get("checked_at"))
            if checked and datetime.now(timezone.utc) - checked <= timedelta(days=max_age):
                return e
        return None

    def _tree(self, key: str, meta: dict[str, Any]) -> dict[str, Any] | str:
        if self._stop.is_set():
            return "deferred"
        cr = self.client.core_remaining
        if cr is not None and cr < int(self.lim.get("min_core_remaining", 1500)):
            self._stop.set()
            return "deferred"
        try:
            return fetch_repo_tree_verdict(self.client, key, meta)
        except RateLimitExhausted:
            self._stop.set()
            return "transient"
        except NotFound:
            return "not_found"
        except Transient:
            return "transient"

    def verdicts(self, metas: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        need = []
        for k, m in metas.items():
            if isinstance(m, str):
                out[k] = m
            elif m.get("fork"):
                out[k] = "fork"
            elif m.get("archived"):
                out[k] = "archived"
            else:
                v = self._cached_verdict(k, m)
                if v:
                    out[k] = v
                    self.repo_outcomes["tree_cache_hit"] += 1
                else:
                    need.append(k)
        need.sort(key=lambda k: -int(metas[k].get("stars") or 0))
        budget = int(self.lim.get("max_tree_fetches", 250))
        window, deferred = need[:budget], need[budget:]
        for k in deferred:
            out[k] = "deferred"
        self.log(f"  trees: cache_hits={self.repo_outcomes['tree_cache_hit']} fetch={len(window)} deferred={len(deferred)}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            for k, v in zip(window, ex.map(lambda k: self._tree(k, metas[k]), window)):
                out[k] = v
                if isinstance(v, dict):
                    self.state.repos[k] = v
        return out

    def _frontmatter(self, owner: str, repo: str, branch: str, skill: dict) -> dict[str, Any]:
        key = skill.get("blob_sha")
        if key and key in self.state.skills:
            return self.state.skills[key]
        text = self.client.raw(owner, repo, branch, skill["skill_path"])
        if text is None:
            info: dict[str, Any] = {"exists": False}
        else:
            fm = parse_skill_frontmatter(text)
            info = {"exists": True, "len": len(text), "frontmatter_license": fm.get("license") or None,
                    "name": fm.get("name") or None, "description": (fm.get("description") or "")[:300] or None}
        if key:
            self.state.skills[key] = info
        return info

    # -- skill level ------------------------------------------------------------
    def evaluate(self, repos: dict[str, dict[str, Any]], metas: dict[str, Any],
                 verdicts: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        max_skills = int(self.lim.get("max_skills_per_repo", 10))
        large = int(self.lim.get("large_repo_threshold", 50))
        for key, entry in sorted(repos.items()):
            v = verdicts.get(key)
            if not isinstance(v, dict):
                outcome = v if isinstance(v, str) else "deferred"
                self.repo_outcomes[outcome] += 1
                self.repo_outcome_by_key[key] = outcome
                continue
            meta = metas[key]
            skills = v.get("skills") or []
            total = int(v.get("skills_total") or len(skills))
            if not skills:
                self.repo_outcomes["missing_skill"] += 1
                self.repo_outcome_by_key[key] = "missing_skill"
                continue
            if large and total > large:
                self.repo_outcomes["large_collection"] += 1
                self.repo_outcome_by_key[key] = "large_collection"
                records.append(self._large_record(key, entry, meta, v, total))
                continue
            self.repo_outcomes["ok"] += 1
            self.repo_outcome_by_key[key] = "ok"
            hints = [o.get("subpath") for o in entry["obs"]]
            eff = [h if (h and any(_covers(h, s["subpath"]) for s in skills)) else None for h in hints]
            picked = [s for s in skills if any(_covers(h, s["subpath"]) for h in eff)][:max_skills]
            owner, _, repo = (v.get("full_name") or meta.get("full_name") or key).partition("/")
            branch = v.get("default_branch") or meta.get("default_branch") or "main"
            for s in picked:
                srcs = [o for o, h in zip(entry["obs"], eff) if _covers(h, s["subpath"])]
                records.append(self._skill_record(key, owner, repo, branch, meta, v, s, total, srcs))
        return records

    def _base_record(self, key: str, owner: str, repo: str, meta: dict, srcs: list[dict]) -> dict[str, Any]:
        lic = meta.get("license") or {}
        return {
            "identity": None, "source_type": "github", "issue": None,
            "repo_url": f"https://github.com/{owner.lower()}/{repo.lower()}",
            "commit_sha": meta.get("commit_sha"), "license_spdx": lic.get("spdx_id"),
            "stars": int(meta.get("stars") or 0), "forks": int(meta.get("forks") or 0),
            "created_at": meta.get("created_at"), "pushed_at": meta.get("pushed_at"),
            "sources": [{"source": o["source"], "source_url": o["source_url"], "observed_at": o["observed_at"],
                         "metrics": o.get("metrics") or {}} for o in srcs],
            "existing_issue": None, "repo_has_open_issue": key in self.issue_repos,
        }

    def _large_record(self, key, entry, meta, v, total) -> dict[str, Any]:
        owner, _, repo = (v.get("full_name") or key).partition("/")
        rec = self._base_record(key, owner, repo, meta, entry["obs"])
        rec.update({"identity": identity_for(owner, repo, None), "status": "large_collection",
                    "skills_total": total, "skill_path": None, "subpath": None,
                    "license_evidence": [], "license_tier": None, "source_url": rec["repo_url"]})
        return rec

    def _skill_record(self, key, owner, repo, branch, meta, v, s, total, srcs) -> dict[str, Any]:
        rec = self._base_record(key, owner, repo, meta, srcs)
        ident = identity_for(owner, repo, s["subpath"])
        folder = s["skill_path"].rsplit("/", 1)[0] if "/" in s["skill_path"] else None
        rec.update({
            "identity": ident, "owner": owner.lower(), "repo": repo.lower(),
            "subpath": _norm_sub(s["subpath"]) or None, "skill_path": s["skill_path"],
            "default_branch": branch, "repo_skills_total": total,
            "source_url": (f"https://github.com/{owner.lower()}/{repo.lower()}/tree/{branch}/{folder}" if folder
                           else f"https://github.com/{owner.lower()}/{repo.lower()}"),
            "existing_issue": self.issue_by_ident.get(ident),
            "license_evidence": [], "license_tier": None, "license": None,
            "_skill": s,
        })
        dup = catalog_match(self.catalog_idx, key, s["subpath"], total)
        if dup:
            rec.update({"status": "already_in_catalog", "catalog_reason": dup})
            return rec
        held = hold_reason(self.holds, ident, key)
        try:
            fm = self._frontmatter(owner, repo, branch, s)
        except GitHubError:
            rec["status"] = "transient"
            return rec
        if not fm.get("exists"):
            rec["status"] = "missing_skill"
            return rec
        rec["skill_name"] = fm.get("name")
        rec["skill_description"] = fm.get("description")
        tier, label, evidence = license_tier(meta, s["subpath"], v.get("license_files") or [],
                                             fm.get("frontmatter_license"))
        rec.update({"license_tier": tier if tier != "reject" else None, "license": label,
                    "license_evidence": evidence})
        if tier == "reject":
            rec["status"] = "missing_license"
            return rec
        if held:
            rec.update({"status": "hold", "hold": held})
            return rec
        rec["status"] = "pending_scan"
        return rec

    def scan_all(self, records: list[dict[str, Any]]) -> None:
        todo = [r for r in records if r.get("status") == "pending_scan"]
        budget = int(self.lim.get("max_scan", 800))
        for r in todo[budget:]:
            r["status"] = "scan_deferred"
        todo = todo[:budget]
        if not self.scan_enabled:
            for r in todo:
                r["status"] = "scan_deferred"
            return

        def one(r):
            try:
                return run_scan_with_client(self.client, {**r, "issue": 0}, self.state, self.rules_hash)
            except Exception as e:  # a scan failure is a hold, never a pass
                return {"status": "hold", "hold": f"scan_error:{type(e).__name__}", "max_severity": None}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            for r, sr in zip(todo, ex.map(one, todo)):
                r["scan"] = {"status": sr.get("status"), "max_severity": sr.get("max_severity"),
                             "finding_counts": sr.get("finding_counts") or {}, "hold": sr.get("hold"),
                             "files_scanned": sr.get("files_scanned"), "rules": self.rules_hash}
                if sr.get("status") == "pass":
                    r["status"] = "pass" if r.get("license_tier") == "pass" else "license_review"
                else:
                    r["status"] = "hold"
                    r["hold"] = sr.get("hold") or "scan_hold"


def public_record(r: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if not k.startswith("_")}
