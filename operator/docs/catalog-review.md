# Catalog review (badge write-back)

Pilot tooling to set `reviewed=true` + Gamora scores on **already indexed** catalog rows.
This is separate from intake `review.py` / `ai_review.py` (those gate *listing*, not the badge).

## Badge rule (`catalog-review/1`)

`reviewed=true` only when **all** of:

1. **S1 deterministic pass**
   - SKILL.md present (catalog `skill_md` or fetchable)
   - name + description present (frontmatter or catalog fields)
   - static scan: **no critical** finding
   - license OK with **SPDX** (`license_tier == pass`; ClawHub → MIT-0)
   - repo/source reachable
2. **S3 AI rubric pass** — same SYSTEM / WEIGHTS / THRESHOLD as `ai_review.py`
   - verdict `pass` **and** Gamora-weighted score **≥ 6.0**

Failing skills **stay listed** with `reviewed=false`; reasons go in `review_evidence` (and the pilot report). Critical findings are flagged for human review. No Drax sandbox claim. No auto-merge.

## Scores

Write all 7 dims plus `average` = Gamora-weighted average **rounded to 1 decimal**.
Optional raw arithmetic mean is stored under `review_evidence.arithmetic_mean`.

## Scripts

| Script | Role |
|--------|------|
| `catalog_review_queue.py` | List `reviewed!=true`, enrich (optional `gh`), rank stars/activity, `--limit` |
| `catalog_review.py s1` | Deterministic checks on queue items |
| `catalog_review.py prepare` | Agent-judged request files (≤8k tokens, no API keys) |
| `catalog_review.py apply` | Merge judged JSON + S1 → review results |
| `catalog_review_apply.py` | Patch catalog **by id/slug only**; refuse if total changes |

### Example (pilot)

```bash
# Queue top 20 unreviewed from a local catalog copy
python3 operator/scripts/catalog_review_queue.py   --catalog /path/skills-catalog.json --limit 20 --out queue.json

# S1 (use --offline only in tests; live pilot should hit gh for SPDX/reachability)
python3 operator/scripts/catalog_review.py s1   --queue queue.json --catalog /path/skills-catalog.json --out s1.json

# Prepare AI requests for S1 passes
python3 operator/scripts/catalog_review.py prepare   --s1 s1.json --catalog /path/skills-catalog.json   --requests-dir requests/ --out prepare-manifest.json

# Agent judges each request-*.json → judged.json {"results":[...]}
python3 operator/scripts/catalog_review.py apply   --judged judged.json --s1 s1.json --out review-results.json --judged-by agent

# Patch catalog (dry-run default; --apply writes)
python3 operator/scripts/catalog_review_apply.py   --catalog /path/skills-catalog.json --results review-results.json   --out skills-catalog.staged.json --delta-out patches.json --apply
```

Live catalog fetch: prefer `curl` with `?cb=$(date +%s%N)` — Python urllib often gets HTTP 403 from proskills.md.

## Catalog fields written

- `reviewed` (bool)
- `scores` (7 dims + `average`)
- `verified_at` (`YYYY-MM-DD` Asia/Dhaka) — set on **pass** only
- `review_version` = `catalog-review/1`
- `review_evidence` — short: verdict, weighted, checks, judged_by, sha, reasons

Never change `id` / `slug` / `repo_url` / `category` / other identity fields. Never add or remove rows.

## Website PR policy

Catalog-only PRs on branch `operator/catalog-review-*`. **No auto-merge** for the pilot.
The hourly publisher merges catalog PRs around :44–:55 Dhaka; review PRs may need a rebase afterward.
