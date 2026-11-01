# Skill folder files

New publishes and the backfill store an optional `files` array on each catalog skill.
Older clients ignore it. `files_found` is still a list of strings.

`catalog_update.py` fetches SKILL.md while staging a **new** candidate and skips identities
already in the catalog, so it never refreshes `skill_md`. `scripts/backfill_skill_files.py`
does that refresh. Nothing else in the publish loop rewrites an existing skill's text.

## Shape

Paths are relative to the skill folder (`skill_path` with a trailing `SKILL.md` removed).
They use forward slashes, no leading slash, and no `..`. The capture never requests or
stores a path outside that folder. A root skill (`skill_path` empty or `SKILL.md`) uses
the repository root and skips `node_modules`, `vendor`, and the other scout excluded
directories.

```json
{
  "files": [
    {"path": "SKILL.md", "size": 6843, "sha": "<40-hex git blob sha>", "content": "---\nname: rederive\n..."},
    {"path": "scripts/history.sh", "size": 3107, "sha": "<blob sha>", "content": "#!/bin/sh\n"},
    {"path": "scripts/structure.py", "size": 9293, "sha": "<blob sha>", "content": "def structure():\n    ...\n"},
    {"path": "assets/logo.png", "size": 12000, "sha": "<blob sha>", "omitted": true},
    {"path": "references/large.md", "size": 40000, "sha": "<blob sha>", "truncated": true, "omitted": true}
  ]
}
```

| Field | When |
| --- | --- |
| `path` | Always. Skill-folder-relative. |
| `size` | Always. Blob size in bytes. |
| `sha` | Always. Git blob SHA-1 from the tree listing. |
| `content` | Only when the full file is text and fits both caps. It is the entire file, not a prefix. |
| `truncated` | `true` when the file is text (or is `SKILL.md` / `README.md`) and larger than the per-file cap. `content` is absent. |
| `omitted` | `true` when `content` is absent: binary, symlink, over the per-file cap, or the per-skill inline budget is used up. Also set when `truncated` is true. |

Missing `content`, `truncated`, and `omitted` mean "not set". A file with `content` does not set those flags.

Caps:

- per file inline: 32 KiB (32768 bytes)
- per skill inline total: 128 KiB (131072 bytes), spent in order `SKILL.md`, `README.md`, `scripts/`, `references/`, `assets/`, then the rest
- legacy `skill_md` and `readme` stay capped at 8000 characters

`id`, `slug`, `author`, `author_url`, `repo_url`, `category`, and an existing `license` / `attribution` are not replaced by a refresh. A missing `license` may be filled from SKILL.md frontmatter.

## Refresh and backfill

A refresh compares the stored `files[].sha` values and `skill_md` with upstream. The same inputs produce the same catalog bytes (idempotent). A truncated tree listing is not written.

If upstream text now matches the content policy (adult/NSFW, piracy, illegal — `content_policy.matches`, the `hold:content-policy` rules), the skill is flagged for review in the state file and the catalog row is not changed.

```bash
python3 operator/scripts/backfill_skill_files.py \
  --catalog website/public/skills-catalog.json \
  --ranking views.csv \
  --preview-slug rederive

python3 operator/scripts/backfill_skill_files.py \
  --catalog website/public/skills-catalog.json \
  --ranking views.csv \
  --apply --website /path/to/website
```

`--dry-run` is the default. It prints the full order and `diff_bytes` (UTF-8 size delta of the catalog JSON) for any `--preview-slug` it fetched. It does not write the state file.

Ranking is CSV `slug,views` or JSON (`[{"slug","views"}]` or `{slug: views}`). Highest views first. Skills absent from the ranking follow, ordered by `github_stars` descending, then slug.

Apply inspects at most `--batch` skills (default 10) serially, one GitHub folder at a time, and opens one PR. The PR branch is `operator/catalog-publish-*`, the only file is `public/skills-catalog.json`, and the body carries `added_count` so the publisher's Asia/Dhaka daily cap of 100 counts the batch. This command does not merge.

State file: `operator/state/skill-files-backfill.json`. After each skill the cursor and any not-yet-published capture are saved. A killed run continues at that cursor and republishes the saved capture. The next full pass, after the previous one finished, refetches and writes nothing when upstream has not changed.

GitHub reads go through `scout.GitHubClient` (30s HTTP timeout). Folder walks stop at `MAX_WALK_DIRS` (200) and at the whole-repo ceilings from open PR #10403 (`MAX_REPO_TREE_CALLS` 400, `TREE_FETCH_BUDGET_S` 120s), read off `scout` when those names exist. This code does not modify `scout.py` or `review.py`, which that PR edits. A subfolder listing is `git/trees/{ref}:{folder}` so sibling directories are not fetched. Tree fetches also stop when the REST core budget falls below scout's `min_core_remaining` (1500).
