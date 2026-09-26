# Source adapters (`operator/scripts/sources/`, `operator/scripts/source_candidates.py`)

Deterministic, stdlib-only adapters that **emit unified skill candidate records (JSON)** from
sources other than submission issues. They **never** write the candidate queue, create or label
issues, stage, publish, merge or message. Wiring these records into intake (for example filing
one issue per candidate for the issue-based review flow) is a separate, later step.

## Sources

| name (`--sources`) | module | what it reads |
|---|---|---|
| `github_topics` | `github_topics.py` | `search/repositories?q=topic:<t>` sorted by stars (topics from config) |
| `new_repos` | `github_new_repos.py` | repos `created:>=now-60d` matching README/topic/name queries. The git tree must confirm a SKILL.md |
| `awesome` | `awesome_lists.py` | raw README of VoltAgent/awesome-agent-skills, ComposioHQ/awesome-claude-skills, travisvn/awesome-claude-skills. `tree/`/`blob/` links keep the subfolder |
| `known_orgs` | `known_orgs.py` | explicit repos plus `org:<org> skill in:name,description,readme` for anthropics, vercel-labs, openai, supabase, microsoft, obra, kepano, … |

Config: `operator/config/sources.json` (topics, queries, lists, orgs, limits, ranking).

## Pipeline (same checks as `scout.py`)

1. Observations `{owner, repo, subpath?, source, source_url, observed_at, metrics}` are merged per repo.
2. Repo metadata via GraphQL (50/query): stars, forks, created/pushed, license, default branch, head commit.
   Forks and archived repos are dropped.
3. Recursive git tree → every SKILL.md (scout's `fetch_repo_tree_verdict`). Verdicts are cached by
   `pushed_at` in `operator/state/sources/repo-cache.json`. The scout's cache is reused read-only.
   Budget `max_tree_fetches` (default 250, highest stars first; the rest is `deferred`), with a
   core-quota guard (`min_core_remaining`).
4. Per skill: identity `github:owner/repo[::subpath]`, subpath-level dedupe vs the live catalog
   (`build_catalog_index`/`catalog_match`), license tier (`pass` / `license_review` / reject →
   `missing_license`). Holds: targets of protected issues (#714 #3644 #4353 #5214 #5226 #5403 #2028 #2029 #2030 #2850),
   critical_static (#2396 #4869), publisher-skip (#2833), and identities held in the candidate queue.
5. Mandatory `static_scan` for every pass/review skill (critical → `hold: critical_static`,
   scan error → hold). Budget `max_scan`. Anything over budget is `scan_deferred`, never pass.
6. Ranking lanes:
   - `evergreen`: stars ≥ 100 and pushed within 365 days. Score = log10(stars+1) + recency bonus
     + 0.3 × (number of sources − 1).
   - `rising`: needs **two dated star readings** at least 20 h apart
     (`operator/state/sources/star-readings.json`, one per UTC day, last 14 kept), and
     ≥ 5 stars/day or ≥ 3 %/day (min 20 stars). Score is based on velocity.
   - `watch`: everything else. The first run has no rising lane (`rising_status: insufficient_readings`).

## Record

```json
{"identity": "github:o/r::skills/x", "source_type": "github", "issue": null,
 "repo_url": "https://github.com/o/r", "source_url": "https://github.com/o/r/tree/main/skills/x",
 "commit_sha": "…", "license_spdx": "MIT", "license": "MIT", "license_tier": "pass",
 "license_evidence": ["repo_spdx:MIT"], "skill_path": "skills/x/SKILL.md", "subpath": "skills/x",
 "default_branch": "main", "stars": 1200, "forks": 80, "created_at": "…", "pushed_at": "…",
 "sources": [{"source": "github_topic:agent-skills", "source_url": "https://github.com/topics/agent-skills",
              "observed_at": "…", "metrics": {"stars": 1200, "rank": 3}}],
 "status": "pass|license_review|hold|already_in_catalog|missing_license|missing_skill|large_collection|transient|scan_deferred",
 "hold": null, "catalog_reason": null, "scan": {"status": "pass", "max_severity": "low", "…": "…"},
 "existing_issue": null, "repo_has_open_issue": false,
 "lane": "evergreen|rising|watch", "score": 3.58, "rising": null, "multi_source_count": 1}
```

`existing_issue` / `repo_has_open_issue` let an issue-filing step skip skills that already have a
submission issue.

## Limits

- Search API: ≥ 2.1 s between search calls (30/min). At most 1000 results per query (paging stops at 10×100).
  Responses are cached for 6 h in `operator/state/sources/http-cache.json`. READMEs are cached for 12 h,
  hold-issue bodies for 24 h.
- Never runs inside the routine windows (:10–:26 and :40–:58 Dhaka) or while a routine lock in
  `operator/state/locks/` is live (`WINDOW_SKIP` / `LOCK_SKIP`). It uses its own private lock
  `operator/state/sources/sources.lock`, so the hourly routines never skip because of it.

## Run

```bash
cd /workspace/proskills-ops
python3 operator/scripts/source_candidates.py                  # dry run -> state/artifacts/sources-dryrun-<stamp>-{candidates,summary}.json
python3 operator/scripts/source_candidates.py --sources awesome,known_orgs --max-tree-fetches 100
python3 operator/scripts/source_candidates.py --write          # source-candidates-YYYY-MM-DD.json (still no queue/issue writes)
```

Tests: `operator/tests/test_sources.py` (mocked HTTP; any real network call fails the test).
