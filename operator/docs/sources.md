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
| `clawhub` | `clawhub.py` | ClawHub feed `https://clawhub.ai/v1/feeds/skills` + public skill pages `https://clawhub.ai/<owner>/skills/<slug>` (see below) |
| `skillsmp` | `skillsmp.py` | SkillsMP official MCP endpoint `POST https://skillsmp.com/mcp`, tool `search_skills` (config queries, `recent`/`stars`). Never the REST `/api/` (robots). See below |
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
   Records are then merged per identity (a renamed repo listed under its old and new name gives one record with both sources).
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

## ClawHub (`clawhub.py`)

- `robots.txt` (checked at runtime, cached 24 h) has `Disallow: /api/` and `Allow: /v1/feeds/skills`. The adapter reads only
  the feed and the public skill pages. It refuses any `/api/…` URL even when robots is unavailable.
  Requests are spaced at least 2 s apart. The feed is cached for 6 h and page extracts for 7 days per skill version.
  At most `clawhub_max_pages` (150) uncached pages are fetched per run; the rest are `pages_deferred`.
- **GitHub-backed skills** (page `githubSourceRepo` + `githubPath`, or a `repository:` GitHub URL in the
  SKILL.md frontmatter) become GitHub observations (`source: clawhub`, metrics `clawhub_downloads/installs/stars`,
  `mapping`). They then go through the normal GitHub checks (the repo license decides, not MIT-0).
- **ClawHub-only skills**: `source_type: clawhub`, identity `clawhub:@owner/slug`, `repo_url` = `source_url` =
  the ClawHub page, `license_spdx: MIT-0`. The license is platform-level; evidence is
  `https://github.com/openclaw/clawhub/blob/HEAD/docs/skill-format.md` ("All skills published on ClawHub are
  licensed under MIT-0"). A page showing a different license gives `license_review`. `stars`/`forks` are `null`.
  Lane `clawhub`, with a score from ClawHub downloads/installs/stars. Dated download readings are stored for a future rising lane.
- **static_scan is mandatory**: the published SKILL.md text comes from the public page (rendered markdown → text)
  and is scanned with `static_scan.scan_candidate`. Holds: `critical_static` (our scan), `unscanned_bundle_scripts`
  (the bundle lists script files, which are reachable only through the disallowed `/api/`, so they cannot be scanned),
  `clawhub_static_critical` (ClawHub's own scanner reported critical findings), `scan_error:skill_md_missing`.
- Catalog dedupe: existing ClawHub listings (`repo_url`/`external_ratings.clawhub_url` on clawhub.ai) are matched by
  owner/slug or slug. Pages are not fetched for listed skills.

## SkillsMP (`skillsmp.py`)

SkillsMP (skillsmp.com) indexes GitHub-hosted SKILL.md skills. Findings (checked 2026-09-26):

- **robots.txt** (`User-agent: *`): `Disallow: /api/`, `/api/github-contents`, `/auth/`; `Crawl-delay: 1`. The REST
  search `GET /api/v1/skills/search` (anonymous 50/day + 10/min per IP, or 500/day with a free key) is therefore
  **not used**. The adapter refuses any `/api/` path and checks robots at runtime (cached 24 h); if robots ever
  disallows `/mcp` it stops (`error: mcp_disallowed_by_robots`).
- **Official MCP server** `POST https://skillsmp.com/mcp` (Streamable HTTP, JSON-RPC 2.0, protocol `2025-06-18`,
  read-only tools `search_skills`, `get_skill`, `list_categories`). SkillsMP recommends it for agents. No API key
  or account, no daily quota; limits 50 POSTs/10 s and 30 `tools/call`/60 s per client IP; 429 carries `Retry-After`.
  `search_skills`: `query` (required), `page` ≤ 50, `limit` ≤ 50, `sortBy` `recent|stars`. Each listing exposes
  `id`, `name`, `author`, `description`, `contentLanguage`, `githubUrl` (`…/tree/<ref>/<path>`), `skillUrl`,
  `stars` (repo stars), `updatedAt`. No license, no SKILL.md text, no catalog-wide change feed.
- **Terms of Service** (https://skillsmp.com/terms, Nov 2025): search/browse allowed; "You may not scrape or
  systematically download large portions of the website"; every skill is subject to its GitHub repo's license.
- **Bounded per run**: one `initialize`, then at most `skillsmp_max_calls` (12) uncached `search_skills` calls,
  ≥ 2.5 s apart, `max_pages` (2) × `per_page` (50) per query. Pages are cached 6 h (`feed` TTL). The query plan
  rotates by UTC hour so no query starves under the cap. Uncached pages over the cap are `pages_deferred`.
- **Errors**: 429 / 5xx / network → one retry (honouring `Retry-After` ≤ 30 s), then the run stops calling SkillsMP
  (`error: transient:…`); nothing is cached, so the next run retries. 4xx / JSON-RPC errors skip that query only.
- **Identity**: a listing whose `githubUrl` parses (`base.parse_github_link`; `tree/`/`blob/` keep the folder)
  becomes a GitHub observation (`source: skillsmp`, metrics `skillsmp_id`, `skillsmp_stars`, `skillsmp_query`,
  `mapping: githubUrl`). It then gets the normal GitHub checks and the identity `github:owner/repo[::subpath]`, so it
  dedups against GitHub/ClawHub-sourced candidates, filed issues and catalog rows; SkillsMP is only provenance in
  `sources[]`. A root `githubUrl` (`…/tree/<ref>`) is a repo-level hint (all SKILL.md, capped by `max_skills_per_repo`).
- **SkillsMP-only listings** (no usable GitHub source): `source_type: skillsmp`, identity `skillsmp:<listing-id>`,
  `status: missing_license`, `hold: skillsmp_only_no_github_source`. The ClawHub-only rules apply (license must pass,
  static scan mandatory), and SkillsMP supplies neither a license nor SKILL.md text, so these never pass.
  `scout_file.py` also refuses any identity other than `github:`/`clawhub:`, so they are never filed.
- **Summary**: `summary.source_breakdown.skillsmp` = listings seen, GitHub-backed vs SkillsMP-only listings,
  identities, `known_in_catalog`, `known_issue` (existing issue or `state/scout/issue-index.json`), `new`,
  `new_fileable`, `also_seen_via_other_sources`, calls / cached / deferred pages.

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
python3 operator/scripts/source_candidates.py --sources skillsmp --skillsmp-max-calls 6   # SkillsMP only, 6 searches
python3 operator/scripts/source_candidates.py --write          # source-candidates-YYYY-MM-DD.json (still no queue/issue writes)
```

Tests: `operator/tests/test_sources.py`, `test_sources_clawhub.py`, `test_sources_skillsmp.py` (mocked HTTP; any real
network call fails the test).

## ClawHub counts on existing listings (`clawhub_stats.py`)

`publish_run.py` refreshes ClawHub download/install/star/comment counts for ClawHub-linked catalog skills in the
staged catalog before building the publish PR (disable with `--no-clawhub-stats`).

### Why the API and not page scraping

- `https://clawhub.ai/robots.txt` (rechecked 2026-09-27): `User-agent: *`, `Disallow: /api/`, `Disallow: /admin/`,
  `Allow: /v1/feeds/plugins`, `Allow: /v1/feeds/skills`.
- ClawHub's own docs (`openclaw/clawhub` `docs/api.md` / `docs/http-api.md`, "Public catalog reuse") say third-party
  directories may use the public read endpoints `GET /api/v1/skills`, `/api/v1/search`, `/api/v1/skills/{slug}`,
  provided they cache, honour 429 / `Retry-After` / rate-limit headers, link to the canonical listing and do not imply
  endorsement. Documented read limit: 3000/min per IP; OpenAPI at `/api/v1/openapi.json`.
- Reading: robots.txt addresses crawlers discovering pages; the documented v1 API is explicitly offered for this kind
  of programmatic, low-volume, cached lookup of known slugs. We are not crawling `/api/` (no link following, no
  enumeration), we call two documented endpoints for listings already in our catalog. So the documented API is the
  compliant path, and it is more accurate than scraping (page scraping could not resolve 40 of 56 listings).
- Only `/api/v1/skills/{slug}` and `/api/v1/search` are allowlisted in `ApiClient`; any other URL raises.

### Request policy

- ≥1 s between requests; at most `--clawhub-stats-max-requests` (default 60) per run and a 180 s wall-clock deadline;
  remaining skills are `deferred` (not touched) and picked up next run, stalest first.
- Up to 3 attempts on timeouts / network errors / 429 / 5xx with exponential backoff and equal jitter (base 2 s,
  cap 30 s); `Retry-After` is honoured (capped at 60 s). 404 is definitive and never retried.
- 409 `AMBIGUOUS_SKILL_SLUG` or an owner mismatch → `/api/v1/search` to pick the listing whose owner matches the owner
  in our ClawHub URL. The owner is only trusted from a ClawHub URL, never guessed from the author field.
- Disk cache (`<state-dir>/clawhub-stats-api-cache.json`, 1 h) stores only definitive answers (200 / 404), never failures.

### Data written (inside `external_ratings`)

| field | meaning |
|---|---|
| `clawhub_stats_status` | `ok` \| `not_found` (ClawHub 404 / no matching listing) \| `fetch_failed` (timeout, 5xx, 429, bad payload) |
| `clawhub_stats_reason` | short machine reason, e.g. `http_503`, `timeout`, `http_404`, `owner_mismatch` |
| `clawhub_fetched_at` | UTC time of the last attempt (any outcome) |
| `clawhub_last_success_at` | UTC time the counts below were retrieved; only set on `ok` |
| `clawhub_downloads/installs/stars/comments` | counts from the last successful read |

Rules: a failed or not-found refresh only updates status/reason/fetched_at. It never writes zero and never overwrites
the counts, `clawhub_last_success_at` or `clawhub_url`. `ok` rows refresh after 24 h, `fetch_failed` after 1 h,
`not_found` after 24 h. Legacy `clawhub_stats_at` / `"unresolved"` are dropped (their provenance was a page scrape,
so the site shows "Unavailable" until the API confirms). The hook runs inside try/except in `publish_run.py`, so
any error in it is reported in the step summary and never blocks publishing. id/slug/category/repo_url are never
touched. Standalone: `python3 operator/scripts/clawhub_stats.py --catalog <website>/public/skills-catalog.json
[--apply] [--max-requests N] [--state-dir DIR]`.

Tests: `operator/tests/test_clawhub_stats.py` (allowlist, backoff/jitter, Retry-After, 404 no-retry, ambiguity,
cache, budget/deadline, never-overwrite for 0/404/429/500/503) and
`test_actions.py::test_clawhub_stats_real_refresh_failure_keeps_last_good_and_publishes`.
