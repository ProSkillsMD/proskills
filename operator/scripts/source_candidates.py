#!/usr/bin/env python3
"""Emit unified skill candidate records from non-issue sources (JSON only).

Sources (operator/scripts/sources/, config in operator/config/sources.json):
  github_topics  GitHub topic search (agent-skills, claude-skills, ...)
  new_repos      repositories created in the last 60 days that look like SKILL.md skills
  awesome        awesome-list README parsers
  known_orgs     known publisher orgs + explicit repos
  clawhub        ClawHub public feed (/v1/feeds/skills) + public skill pages (never /api/, per robots.txt)

Every observation goes through the scout's checks (git-tree SKILL.md discovery, subpath-level
dedupe vs the live catalog, license tiers, mandatory static_scan, protected/critical/publisher-skip
holds) and comes out as a record:

  {identity, source_type, repo_url, source_url, commit_sha, license_spdx, license_evidence,
   license_tier, skill_path, subpath, stars, forks, created_at, pushed_at,
   sources:[{source, source_url, observed_at, metrics}], issue: null, existing_issue,
   status, hold, scan, lane, score, rising, ...}

This script NEVER writes the candidate queue, creates issues, stages, publishes, merges, labels or
messages. Default --dry-run writes operator/state/artifacts/sources-dryrun-<stamp>-{candidates,summary}.json;
--write writes source-candidates-YYYY-MM-DD.json (+ summary) for a later, separate intake step.
State (search/README cache, tree/scan caches, dated star readings) lives in operator/state/sources/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
from scout import AdaptiveThrottle, GitHubClient, ScoutState, get_gh_token  # noqa: E402
from sources import awesome_lists, clawhub, github_new_repos, github_topics, known_orgs  # noqa: E402
from sources.base import STATE, DiskCache, RepoSearch, SearchLimiter, iso, load_config, utc_now  # noqa: E402
from sources.evaluate import (SourceEvaluator, existing_issue_index, fetch_meta, load_hold_rules,  # noqa: E402
                              merge_observations, public_record)
from sources.rank import assign_lane, record_reading  # noqa: E402

ALL_SOURCES = ("github_topics", "new_repos", "awesome", "known_orgs", "clawhub")
STATUS_KEYS = ("pass", "license_review", "hold", "already_in_catalog", "missing_license", "missing_skill",
               "large_collection", "transient", "scan_deferred")
ROUTINE_WINDOWS = ((10, 26), (40, 58))  # Dhaka minutes around intake (:14) and publisher (:44)


def source_family(source: str) -> str:
    return source.split(":", 1)[0]


def summarize(records: list[dict[str, Any]], repo_outcome_by_key: dict[str, str],
              obs_by_source: dict[str, set[str]]) -> dict[str, Any]:
    """Counts per source (and per family): candidates (skill records), pass, license_review, holds,
    already_in_catalog, other statuses, plus repo-level outcomes (not_found/transient/deferred/...)."""
    per: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        for name in {s["source"] for s in r.get("sources") or []} | {source_family(s["source"]) for s in r.get("sources") or []}:
            c = per[name]
            c["candidates"] += 1
            st = r.get("status") or "unknown"
            c["holds" if st == "hold" else st] += 1
    groups: dict[str, set[str]] = defaultdict(set)
    for src, keys in obs_by_source.items():
        groups[src] |= keys
        groups[source_family(src)] |= keys
    for name, keys in groups.items():
        per[name]["repos_observed"] = len(keys)
        for k in keys:
            o = repo_outcome_by_key.get(k)
            if o and o != "ok":
                per[name][f"repos_{o}"] += 1
    return {k: dict(v) for k, v in sorted(per.items())}


def in_routine_window(now: datetime) -> bool:
    m = now.minute
    return any(a <= m <= b for a, b in ROUTINE_WINDOWS)


def live_routine_lock() -> dict | None:
    for p in scout.LOCKS.glob("*.lock"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = data.get("pid")
        if pid and Path(f"/proc/{pid}").exists():
            return {"path": p.name, "routine": data.get("routine")}
    return None


@contextmanager
def sources_lock(root: Path):
    """Private lock (NOT in state/locks, so hourly routines never LOCK_SKIP because of us)."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / "sources.lock"
    if p.exists():
        try:
            pid = int(json.loads(p.read_text()).get("pid") or 0)
        except Exception:
            pid = 0
        if pid and Path(f"/proc/{pid}").exists():
            raise SystemExit(f"sources state busy (pid {pid})")
    import os
    p.write_text(json.dumps({"pid": os.getpid(), "at": iso()}))
    try:
        yield
    finally:
        p.unlink(missing_ok=True)


def collect_observations(selected: list[str], cfg: dict[str, Any], search: RepoSearch, raw_text) -> tuple[list[dict], dict]:
    obs: list[dict] = []
    info: dict[str, Any] = {}
    if "github_topics" in selected:
        o = github_topics.collect(search, cfg["github_topics"], int(cfg.get("topic_max_pages", 3)))
        info["github_topics"] = {"observations": len(o)}
        obs += o
    if "new_repos" in selected:
        nr = cfg["new_repos"]
        o = github_new_repos.collect(search, nr["queries"], int(nr.get("created_within_days", 60)),
                                     int(nr.get("max_pages", 2)))
        info["new_repos"] = {"observations": len(o)}
        obs += o
    if "awesome" in selected:
        o, meta = awesome_lists.collect(raw_text, cfg["awesome_lists"])
        info["awesome"] = {"observations": len(o), "lists": meta}
        obs += o
    if "known_orgs" in selected:
        ko = cfg["known_orgs"]
        o = known_orgs.collect(search, ko["orgs"], ko.get("repos") or [], ko.get("org_query") or "skill",
                               int(ko.get("org_max_results", 50)))
        info["known_orgs"] = {"observations": len(o)}
        obs += o
    return obs, info


def make_raw_text(client: GitHubClient, cache: DiskCache, ttl_s: float):
    def fetch(owner: str, repo: str, path: str) -> str | None:
        key = f"raw:{owner}/{repo}:{path}".lower()
        hit = cache.get(key, ttl_s)
        if hit is not None:
            return hit or None
        text = None
        for ref in ("HEAD", "main", "master"):
            text = client.raw(owner, repo, ref, path)
            if text is not None:
                break
        cache.put(key, text or "")
        return text
    return fetch


def cached_meta(client: GitHubClient, cache: DiskCache, keys: list[str], ttl_s: float) -> dict[str, Any]:
    """fetch_meta with a per-repo disk cache (only dict/not_found results are cached; transient is retried)."""
    out: dict[str, Any] = {}
    missing = []
    for k in keys:
        v = cache.get(f"meta:{k}", ttl_s)
        if v is None:
            missing.append(k)
        else:
            out[k] = v
    for i in range(0, len(missing), 500):  # chunked so autosave keeps progress on a long first run
        got = fetch_meta(client, missing[i:i + 500])
        for k, v in got.items():
            if v != "transient":
                cache.put(f"meta:{k}", v)
        out.update(got)
    return out


def run(args: argparse.Namespace, *, client: GitHubClient | None = None, catalog: dict | None = None,
        state_dir: Path = STATE, art: Path = scout.ART, now: datetime | None = None,
        extra_observations: list[dict] | None = None, sleep=time.sleep,
        clawhub_fetch=None) -> dict[str, Any]:
    cfg = load_config(Path(args.config)) if getattr(args, "config", None) else load_config()
    lim = {**cfg.get("limits", {})}
    for k in ("max_tree_fetches", "max_scan"):
        if getattr(args, k, None) is not None:
            lim[k] = getattr(args, k)
    ttl = {k: float(v) * 3600 for k, v in (lim.get("cache_ttl_hours") or {}).items()}
    now = now or utc_now()
    client = client or GitHubClient(get_gh_token(), throttle=AdaptiveThrottle(6))
    catalog = catalog if catalog is not None else scout.load_catalog_live(getattr(args, "catalog", None))
    persist = not getattr(args, "no_persist_state", False)
    cache = DiskCache(state_dir / "http-cache.json", autosave_every=int(lim.get("cache_autosave_every", 25)) if persist else 0)
    t0 = time.monotonic()

    def stage(name: str, **kw) -> None:
        print(f"[sources] {time.monotonic() - t0:6.1f}s {name} " + " ".join(f"{k}={v}" for k, v in kw.items()), flush=True)

    search = RepoSearch(client, cache, SearchLimiter(float(lim.get("search_min_interval_s", 2.1)), sleep=sleep),
                        ttl_s=ttl.get("search", 6 * 3600))
    selected = [s for s in (args.sources.split(",") if args.sources else ALL_SOURCES) if s in ALL_SOURCES]
    obs, info = collect_observations(selected, cfg, search, make_raw_text(client, cache, ttl.get("raw_list", 12 * 3600)))
    stage("github_sources_done", observations=len(obs), search_calls=search.limiter.calls)
    obs += list(extra_observations or [])
    clawhub_records: list[dict] = []
    if "clawhub" in selected:
        ch = clawhub.ClawHubClient(cache, fetch=clawhub_fetch or clawhub.default_fetch,
                                   min_interval=float(lim.get("clawhub_min_interval_s", 2.0)), sleep=sleep,
                                   feed_ttl_s=ttl.get("feed", 6 * 3600))
        max_pages = getattr(args, "clawhub_max_pages", None)
        ch_obs, clawhub_records, ch_info = clawhub.collect(
            ch, catalog, max_pages=int(max_pages if max_pages is not None else lim.get("clawhub_max_pages", 150)),
            scan=not getattr(args, "no_scan", False), now_iso=iso(now))
        info["clawhub"] = {"observations": len(ch_obs), **ch_info}
        obs += ch_obs
        stage("clawhub_done", observations=len(ch_obs), clawhub_only=len(clawhub_records))
    repos = merge_observations(obs)
    obs_by_source: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        obs_by_source[o["source"]].add(f"{o['owner']}/{o['repo']}".lower())
    metas = cached_meta(client, cache, sorted(repos), ttl.get("meta", 6 * 3600))
    stage("meta_done", repos=len(repos))
    state = ScoutState(state_dir)
    fallback = scout._load_json(scout.SCOUT_STATE / "repo-cache.json", {}) if not getattr(args, "no_scout_cache", False) else {}
    ev = SourceEvaluator(client, state, catalog, hold_rules=load_hold_rules(client, cache, art, ttl.get("issues", 86400)),
                         limits=lim, issue_index=existing_issue_index(art), fallback_repo_cache=fallback,
                         scan=not getattr(args, "no_scan", False), log=print)
    verdicts = ev.verdicts(metas)
    stage("verdicts_done", verdicts=len(verdicts))
    records = ev.evaluate(repos, metas, verdicts)
    ev.scan_all(records)
    stage("scan_done", records=len(records))
    readings = scout._load_json(state_dir / "star-readings.json", {})
    rank_cfg = cfg.get("ranking") or {}
    for k, m in metas.items():
        if isinstance(m, dict):
            record_reading(readings, k, int(m.get("stars") or 0), now, int(rank_cfg.get("readings_kept", 14)))
    for r in records:
        rk = r["repo_url"].split("github.com/", 1)[-1].lower()
        assign_lane(r, readings.get(rk), rank_cfg, now)
    for r in clawhub_records:  # ClawHub-only: lane "clawhub" (score from ClawHub stats); keep dated download readings
        d = ((r.get("sources") or [{}])[0].get("metrics") or {}).get("clawhub_downloads")
        if d is not None:
            record_reading(readings, r["identity"], int(d), now, int(rank_cfg.get("readings_kept", 14)))
    records += clawhub_records
    records.sort(key=lambda r: ({"pass": 0, "license_review": 1}.get(r.get("status"), 2),
                                {"rising": 0, "evergreen": 1}.get(r.get("lane"), 2), -float(r.get("score") or 0),
                                r.get("identity") or ""))
    if persist:
        state.save()
        cache.save()
        scout._atomic_write(state_dir / "star-readings.json", readings)
    pub = [public_record(r) for r in records]
    summary = {
        "generated_at": iso(now), "sources_selected": selected, "observations": len(obs),
        "repos_observed": len(repos), "records": len(pub),
        "status_counts": dict(Counter(r.get("status") for r in pub)),
        "lanes": dict(Counter(r.get("lane") for r in pub if r.get("status") in ("pass", "license_review"))),
        "per_source": summarize(records, ev.repo_outcome_by_key, obs_by_source),
        "repo_outcomes": dict(ev.repo_outcomes), "source_info": info,
        "search_calls": search.limiter.calls, "search_errors": search.errors[:20],
        "cache": {"hits": cache.hits, "misses": cache.misses},
        "api_calls": dict(client.calls), "core_remaining": client.core_remaining,
        "catalog_count": len(catalog.get("skills") or []),
        "note": "records only; no queue writes, no issues, no staging/publishing",
    }
    return {"summary": summary, "candidates": pub}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Emit unified skill candidate records from non-issue sources")
    ap.add_argument("--sources", default=None, help=f"comma list from {','.join(ALL_SOURCES)} (default all)")
    ap.add_argument("--write", action="store_true", help="write source-candidates-YYYY-MM-DD.json (default dry run)")
    ap.add_argument("--catalog", type=Path, default=None, help="catalog snapshot (default live)")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--max-tree-fetches", type=int, default=None)
    ap.add_argument("--max-scan", type=int, default=None)
    ap.add_argument("--no-scan", action="store_true", help="skip static_scan (records stay scan_deferred)")
    ap.add_argument("--no-persist-state", action="store_true")
    ap.add_argument("--no-scout-cache", action="store_true", help="do not reuse the scout's repo verdict cache")
    ap.add_argument("--clawhub-max-pages", type=int, default=None,
                    help="max uncached ClawHub skill pages fetched this run (default config, 150)")
    ap.add_argument("--ignore-routine-windows", action="store_true")
    args = ap.parse_args(argv)
    dhaka_now = datetime.now(scout.DHAKA)
    if not args.ignore_routine_windows and in_routine_window(dhaka_now):
        print(f"WINDOW_SKIP: {dhaka_now:%H:%M} Dhaka is inside a routine window {ROUTINE_WINDOWS}", flush=True)
        return 0
    held = live_routine_lock()
    if held:
        print(f"LOCK_SKIP: live routine lock {held}", flush=True)
        return 0
    with sources_lock(STATE):
        out = run(args)
    stamp = dhaka_now.strftime("%Y-%m-%d-%H%M")
    scout.ART.mkdir(parents=True, exist_ok=True)
    if args.write:
        cpath = scout.ART / f"source-candidates-{dhaka_now.date().isoformat()}.json"
        spath = scout.ART / f"source-candidates-{stamp}-summary.json"
    else:
        cpath = scout.ART / f"sources-dryrun-{stamp}-candidates.json"
        spath = scout.ART / f"sources-dryrun-{stamp}-summary.json"
    scout._atomic_write(cpath, {"summary": out["summary"], "candidates": out["candidates"]})
    scout._atomic_write(spath, out["summary"])
    print(json.dumps({k: out["summary"][k] for k in ("observations", "repos_observed", "records", "status_counts",
                                                     "lanes", "repo_outcomes", "search_calls")}, indent=2))
    print(f"wrote {cpath}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
