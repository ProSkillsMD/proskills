# Committed intake scout (`operator/scripts/scout.py`)

Replaces the untracked `/tmp/intake_enrich_HHMM.py` that the hourly intake regenerated
every run. Stdlib only, never executes candidate code, never stages/publishes/merges/
labels/messages.

## What it does per run

1. Lists **every** open issue (`GET /repos/ProSkillsMD/proskills/issues`, ~23 REST calls).
2. Cheap filters: protected (`PROTECTED_ISSUES`), blocked labels, known holds
   (`STATIC_HOLDS` = #2396, #4869 + `candidate-queue-2026-09-25.pre-scan.json` + the current
   queue's `holds_critical_static`), no GitHub URL, identical target (oldest issue wins).
3. Repo metadata for **all** target repos via GraphQL (50 repos/query, follows renames):
   `pushedAt`, default branch, stars, `licenseInfo`. GraphQL `NOT_FOUND` → `not_found`.
4. Recursive git tree (`/git/trees/{branch}?recursive=1`) only for repos whose cached
   verdict is missing or whose `pushed_at` changed, over a **rotating window**
   (`--max-tree-fetches`, default 600). Order: repos that were transient last run →
   repos from issues opened since the last run → rotation by issue number after the cursor.
   Cursor + caches live in `operator/state/scout/` (gitignored):
   `cursor.json`, `repo-cache.json` (keyed to `pushed_at`), `skill-cache.json`
   (SKILL.md frontmatter by blob sha), `scan-cache.json` (by skill folder tree sha +
   static_scan.py hash).
5. Every `SKILL.md` at any depth (`skills/*`, `.claude/skills/*`, `.agents/skills/*`,
   `plugins/*/skills/*`, …; `node_modules`/`vendor`/`dist`/fixtures excluded), root first,
   capped per issue (`--max-skills-per-repo`, default 25). Truncated trees also probe the
   conventional paths via raw. If the default branch has no SKILL.md, `main`/`master` are
   tried (same fallback the old raw probe had). Repos with more than
   `--large-repo-threshold` (default 50) SKILL.md files — aggregators/registries such as
   `majiayu000/claude-skill-registry` (21k) — are not expanded per skill; the issue gets
   outcome `large_collection` and is listed under `large_collections` for human review.
6. Identity per skill folder: `github:owner/repo` (root SKILL.md) or
   `github:owner/repo::subpath`. Catalog dedupe (`build_catalog_index`):
   - subpath listed (`skill_path` / `files_found`) → duplicate;
   - whole-repo listing → duplicate for the root skill, for the only skill of a single-skill
     repo, and for every skill of an `is_collection` listing; **distinct subfolder skills
     of a multi-skill repo are new**.
7. License tiers:
   - `pass`: repo SPDX id (not NONE/NOASSERTION);
   - `license_review`: NOASSERTION, unclassified root license file, LICENSE in the skill
     folder, or `license:` in SKILL.md frontmatter (evidence recorded) — **never in `passed`**;
   - reject (`missing_license`): no evidence anywhere.
8. Mandatory `static_scan` (text only) for every pass/review skill; critical → hold
   `critical_static`; scan error → hold `scan_error:*`.

## Failure handling

| Outcome | Cause | Recorded as rejection? | Cached? |
|---|---|---|---|
| `not_found` | 404/410, GraphQL NOT_FOUND | yes (`repo_fail` compat key) | no |
| `transient` | 403 rate limit, 429, 5xx, timeout, network, plain 403 | **no** | **no** (retried first next run) |
| `deferred` | outside this run's window / budget guard hit | no | no |

Retries use exponential backoff with jitter, honour `Retry-After` and
`X-RateLimit-Reset`; if the reset is > `max_wait` (120 s) the run stops tree work
(`rate_limit_exhausted: true`) and defers the rest. Each rate-limit event halves the
worker concurrency (recovers slowly). `--min-core-remaining` (default 1500) stops tree
fetches before the REST core quota is drained so other routines keep headroom.

## Outputs

`--dry-run` (default): `scout-dryrun-<stamp>-{queue-preview,summary,scan-results,parent-result}.json`.

`--write` (routine mode): same artifact set as the /tmp scout —
`candidate-queue-YYYY-MM-DD.json`, `intake-<stamp>-{summary,eligible,scan-results,batch-candidates,parent-result}.json`
and a section in `operator/logs/intake-YYYY-MM-DD.md`. Queue keys are unchanged
(`eligible`, `passed`, `batch_seed`, `publisher_skip_batch_seed`, `holds_critical_static`,
`stats`, …) plus additive keys `license_review`, `skill_stats`, `tiers`, `coverage`,
`top_repos_by_new_skills`, `passed_total_before_trim`. `passed` entries carry full
candidate fields (`identity`, `subpath`, `skill_path`, `default_branch`, `repo_in_catalog`,
`queue_rank`, …).

### Passed-queue order (`order_passed`)

`passed` is capped at 40 (`--queue-max-passed`) with **at most 5 skills per repo**
(`--queue-max-per-repo`) and is already in publish order:

1. group by issue (issue-less candidates from future sources: one group per repo), inside a
   group by stars desc;
2. groups ordered by their best star count; **round-robin**: round r takes the r-th skill of
   every group, so one issue / monorepo cannot take all top slots (no pure star sort);
3. candidates whose repo already has a catalog listing (`repo_in_catalog: true`) come after all
   others, because the publisher and the website validator (`duplicate normalized repos`)
   currently allow one listing per GitHub repo;
4. duplicate identities are dropped.

### Identity-keyed queue helpers (for the publisher)

Queue entries are keyed by skill identity `github:owner/repo[::subpath]`, never by issue number,
so sibling skills of one issue survive when one of them is published.

- `merge_passed_with_eligible(queue)` → full records for `passed`, merged with `eligible` by identity.
- `remove_identities_from_queue(queue, identities)` → pure function.
- `remove_published_from_queue(identities, queue_path=None, run_id=None)` → atomic rewrite of the
  queue file (default today's `candidate-queue-YYYY-MM-DD.json`).
- CLI: `python3 operator/scripts/scout.py --remove-identity github:o/r::skills/x [--remove-identity …] [--run-id hourly-…]`
  (queue maintenance only, no scouting).

`stats` keeps the old issue-level keys; `repo_fail` now equals `not_found` only, and
`license_review`, `not_found`, `transient`, `deferred` are added.

## Adopting it in routines

Intake routine — replace "write /tmp/intake_enrich_HHMM.py and run it" with:

```bash
cd /workspace/proskills-ops && python3 operator/scripts/scout.py --write --run-id intake-$(TZ=Asia/Dhaka date +%F-%H%M)
```

Hourly publisher: load the committed module instead of `/tmp/intake_enrich_*.py`. Preferred:

```python
sys.path.insert(0, "/workspace/proskills-ops/operator/scripts")
import scout as _intake
```

`spec_from_file_location(...)` + `exec_module` also works now (the module registers a
`sys.modules` shim; previously `@dataclass` crashed with `'NoneType' object has no attribute '__dict__'`).
The compat names (`classify_batch`, `run_scan`, `rematerialize_warm`, `public_cand`, `get_gh_token`,
`fetch_issues_pass`, `load_known_holds`) are unchanged.

### Stuck publish PR check (`operator/scripts/merge_stuck_publish_pr.py`)

The old intake's "merge a website catalog publish PR stuck > 90 min" check, now a standalone
committed script for the publisher. Dry run by default; `--apply` merges. Merges only open,
non-draft PRs to `main` from `operator/catalog-publish-*` by Asif2BD, never #29 or #1-15,
created > 90 min ago, diff exactly `public/skills-catalog.json`, `MERGEABLE` + `CLEAN`, and no
failing/pending check in `gh pr checks` (this token gets 403 on checks for the website repo, so
GitHub's `CLEAN` state is the recorded evidence: `checks_source: merge_state_clean`). Squash with
`--match-head-commit`. `--out FILE` also writes the JSON summary.

### catalog_update.py and subfolder skills

- id/slug: `owner-repo-sub-path` (suffix `-2`, … on collision); existing ids/slugs never change.
- `repo_url` stays the bare repo (the website builds raw/download URLs from it and dedupes one
  listing per repo); new `source_url` = subfolder tree URL `https://github.com/o/r/tree/<branch>/<folder>`;
  `skill_path` keeps the original-case folder.
- raw fetches try the candidate's `default_branch` first, then main/master; a subfolder skill's
  own README is preferred over the repo README.
- delta JSON gains `added_map` (`identity`, `issue`, `id`, `slug`, `category`, `source_url`) so the
  publisher maps published skills to issues by identity instead of guessing by owner/repo.

## Tests

`operator/tests/test_scout.py` (mocked transport; a guard fails the test on any real
network call). Run: `cd operator && python3 -m unittest discover -s tests -v`.
