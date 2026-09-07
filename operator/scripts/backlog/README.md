# Backlog dry-run

Deterministic Python tool to classify open submission issues. **No GitHub writes.**
Dry-run only: reads local JSON dumps (issues + catalog) and writes local manifests.

```bash
python3 build_backlog_dry_run.py \
  --issues /path/to/open-submissions.json \
  --catalog /path/to/skills-catalog.json \
  --out-dir ./out
```

## Dispositions (mutually exclusive)

| Disposition | Meaning |
|---|---|
| `canonical_actionable` | Earliest open issue (by `createdAt`, then lowest number) for a normalized source **not** in the live catalog |
| `normalized_source_duplicate` | Later open issue sharing the same normalized source as a canonical sibling (GitHub untouched; mapping preserved) |
| `already_published` | Normalized source already present in the live skills catalog |
| `missing_source` | No extractable source URL in title/body |
| `invalid_source` | URLs present but none normalize to a usable repo identity |

Ambiguous / manual_review is reserved for future rules; current classifier does not emit it.

## URL normalization (conservative)

Implemented in `normalize.py`:

- Prefer first GitHub repo URL found in body/title; else first non-empty normalized URL
- Strip query, fragment, trailing slash, and `.git`
- Lowercase GitHub `owner` / `repo`
- Accept `https://`, `http://`, `git@github.com:`, `git+https://`, and `owner/repo` shorthand
- **Do not** merge distinct repos by similar titles
- **Monorepo subpath rule:** GitHub URLs with extra path segments (`/tree/...`, `/blob/...`, nested skill folders, etc.) normalize to **`https://github.com/owner/repo` only**. Subpaths are intentionally discarded so one repo identity matches the catalog; different repos never collapse together.

## Outputs (local; `out/` gitignored)

- `backlog-cleanup-manifest.json` — per-issue disposition + reason + canonical mapping (full; do not commit)
- `backlog-cleanup-summary.json` / `.csv` — aggregate counts (safe to commit if desired)
- `backlog-execution-manifest.json` — issue numbers only, grouped by disposition (local/gitignored)
- `backlog-rollback-manifest.json` — dry-run: empty `issue_numbers_touched` (no GitHub mutations)

Do not commit full issue dumps, bodies, emails, credentials, or PII.
