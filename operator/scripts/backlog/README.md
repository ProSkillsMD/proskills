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
| `canonical_actionable` | Earliest open issue (by `createdAt`, then lowest number) for a normalized source identity **not** already published |
| `normalized_source_duplicate` | Later open issue sharing the same normalized source identity as a canonical sibling |
| `already_published` | Repository identity **and** explicit subpath (when available) support a match to the live catalog |
| `missing_source` | No extractable source URL in title/body |
| `invalid_source` | URLs present but none normalize to a usable identity |
| `ambiguous_manual_review` | Competing identities in one issue, or publish alignment is unclear |

## URL / identity rules

Implemented in `normalize.py` + `build_backlog_dry_run.py`:

### Candidate extraction

- Extract **all** candidate source URLs from body/title (not first-URL-only)
- Each candidate normalizes to `{repo, subpath, identity_key}`
- If multiple URLs normalize to the **same** `identity_key` → classify normally
- If they normalize to **competing** identities → `ambiguous_manual_review`

### Repository vs skill identity (monorepo)

| Precedence | Rule |
|---|---|
| 1 | `owner/repo` is the **repository identity** |
| 2 | Explicit GitHub `tree`/`blob` path after the ref is a **secondary skill identity** (`subpath`) |
| 3 | Dedupe key = `repo::subpath` (empty subpath = root-repository submission) |
| 4 | Two different explicit subpaths in one monorepo are **not** duplicates |
| 5 | Root-repository submissions may match **root-repository** catalog records |
| 6 | Root catalog records must **not** automatically absorb distinct explicit-subpath submissions into `already_published` |
| 7 | Canonical among an identity group: earliest `createdAt`, then lowest issue number |

### already_published

- Match the live catalog deterministically by repository URL
- Require subpath alignment when the issue (or catalog record) carries an explicit subpath
- Unclear alignment (e.g. root issue vs catalog that only lists explicit subpaths) → `ambiguous_manual_review`
- Multiple issues may be `already_published` for the same live skill; manifests keep `published_skill_ids`

### Normalization details

- Strip query, fragment, trailing slash, and `.git`
- Lowercase GitHub `owner` / `repo`; lowercase subpath for stable compare
- Accept `https://`, `http://`, `git@github.com:`, `git+https://`, and `owner/repo` shorthand
- Non-`tree`/`blob` GitHub extras (`/issues/...`, etc.) collapse to repository root only

## Outputs (local; `out/` gitignored)

- `backlog-cleanup-manifest.json` — per-issue disposition + reason + normalized candidates (no issue bodies / PII; do not commit)
- `backlog-cleanup-summary.json` / `.csv` — aggregate counts
- `backlog-execution-manifest.json` — issue numbers only, grouped by disposition
- `backlog-rollback-manifest.json` — dry-run: empty `issue_numbers_touched`

Do not commit full issue dumps, bodies, emails, credentials, or PII.
