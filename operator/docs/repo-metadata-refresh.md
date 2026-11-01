# Repo metadata refresh

`catalog_update.py` appends new skills and then skips any identity already in the catalog.
`backfill_skill_files.py` refreshes `skill_md` and `files` only.
`clawhub_stats.py` refreshes ClawHub counts only.
The website builder `scripts/catalog/build_catalog.py` keeps the previous `github_stars`,
`last_commit`, and `readme` instead of reading GitHub again. After the first publish those
fields stay at whatever was true that day.

`scripts/repo_metadata_refresh.py` re-reads GitHub for existing GitHub-backed rows and writes
the fields back onto `public/skills-catalog.json`, which is the file the builder preserves.
The reference row is `ericjujianzou-rederive-skills-rederive` after website PR #368:
378 stars, `version` `0.1.0` from `.claude-plugin/plugin.json`, `last_commit`
`2026-10-25T20:26:46Z` (default-branch committer date, not `pushed_at`), `open_issues` 0,
`homepage` `https://ericzou.dev/rederive/`, `author_avatar` / `author_github`, and
`last_synced_at`.

## Fields written

| Field | Source |
| --- | --- |
| `github_stars` | `stargazerCount` |
| `last_commit` | default-branch tip `committedDate`, UTC `Z` |
| `open_issues` | open issues only (`issues(states: [OPEN]).totalCount`), not pull requests |
| `homepage` | repository `homepageUrl`, when GitHub has one. An empty homepage does not clear a value already on the row |
| `author_avatar`, `author_github` | owner avatar and `https://github.com/<login>` (login case preserved), only when the row's value is missing or blank |
| `version` | first real version in `.claude-plugin/plugin.json` (skill folder, then repo root), `plugin.json`, `package.json`, then `SKILL.md` frontmatter. `0.0.0` and the other placeholders are not real and are never written |
| `readme` | skill-folder `README.md`, else repo `README.md`, capped at 8000 characters (same cap as `catalog_update.py`). Written only when that text differs from the stored field |
| `last_synced_at` | UTC time of this write. Set when another field changed, or when it was missing after a successful read. A later run that finds the same upstream leaves the previous timestamp in place |

`id`, `slug`, `category`, `reviewed`, `scores`, and every other field (including `works_with`,
`repo_url`, `skill_md`, and `files`) are not modified.

The same inputs produce the same catalog bytes. A second run against unchanged upstream is a
no-op, including `readme` and `last_synced_at`.

## Deleted, renamed, and private repos

GitHub queries are batched GraphQL through `scout.GitHubClient` (the same client and retry
behavior as the sources pipeline). One repository lookup follows a rename. The new
`nameWithOwner` is recorded in the state file and the catalog row stays, including its
`repo_url`. A repository GitHub will not return (deleted, private, or missing) is recorded
under `unavailable` and the row is left as it is. Nothing is deleted. A transient
failure stays pending for one retry. If it fails again on the next run, the row is
left unchanged and the pass moves on, so one unreachable repository cannot block
every later batch.

## Order, limit, resume

```bash
python3 operator/scripts/repo_metadata_refresh.py \
  --catalog website/public/skills-catalog.json \
  --ranking operator/state/traffic-ranking-2026-11-01.csv \
  --limit 20

python3 operator/scripts/repo_metadata_refresh.py \
  --catalog website/public/skills-catalog.json \
  --ranking operator/state/traffic-ranking-2026-11-01.csv \
  --slug claude-skills --slug anthropic-audit-support \
  --limit 5
```

The ranking file is the same CSV (`slug,views`) or JSON that `backfill_skill_files.py` uses.
GitHub-backed rows are ordered highest views first. Rows that are not in the file follow, by
`github_stars` descending, then slug. Non-GitHub rows are skipped and do not use the limit.

`--limit` (default 20) is how many pending rows this run inspects. Progress is
`operator/state/repo-metadata-refresh.json`. A killed apply continues at the next pending row.
A finished pass starts over and writes nothing when upstream has not changed. Dry-run reads
that cursor but does not write the state file. `--slug` restricts the run to those slugs
without discarding the saved cursor for the others.

## Apply

`--apply` needs `--website` (a checkout of `ProSkillsMD/website`). It commits only
`public/skills-catalog.json` on `operator/catalog-publish-<timestamp>` and opens a PR.
`merge_stuck_publish_pr.py` and the hourly publisher only squash-merge that prefix, and only
when the diff is catalog-only, so the branch matches the existing merge tooling.
`operator/catalog-refresh-*` is recognized only as another open publish head to avoid
starting a second batch. This command does not merge.

The PR body sets `added_count` to 0. The refresh is not new skills, so it does not consume
the publisher's 100-per-Asia/Dhaka-day cap. The title has no `+N skills` clause for the same
reason. The publish marker carries no issue ids, so reconcile will not close candidate issues.

Two gates, both the same idea as `backfill_skill_files.py`:

- Do not open a PR while any `operator/catalog-publish-*` or `operator/catalog-refresh-*` PR is still open.
- Do not open a PR between minute :44 and minute :58 Asia/Dhaka inclusive. That is the publisher window. Dry-run is allowed then. This command never merges.

```bash
python3 operator/scripts/repo_metadata_refresh.py \
  --catalog website/public/skills-catalog.json \
  --ranking operator/state/traffic-ranking-2026-11-01.csv \
  --limit 20 \
  --apply --website /path/to/website
```
