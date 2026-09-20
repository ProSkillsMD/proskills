# Publish canary (operator)

Dry-run-by-default Python path to discover submission candidates, stage catalog
appends, and (optionally) open a **website** PR. **Never merges. No AI in scripts.**

## Scripts

| Script | Role |
|--------|------|
| `scripts/discover.py` | Scout open issues → candidates JSON; skip protected / blocked / already published |
| `scripts/catalog_update.py` | APPEND-only staged `skills-catalog.staged.json` + `delta.json` |
| `scripts/create_pr.py` | Copy staged catalog into website clone, branch, push, `gh pr create` (never merge) |
| `scripts/reconcile.py` | Read-only live vs staged / candidates drift report |
| `scripts/publish_canary.py` | Orchestrate discover subset → catalog_update → verification checklist URLs |

Shared helpers: `scripts/publish_lib.py` (identity `github:owner/repo[::subpath]`, slugify, fetch).

## Identity / dedupe

- Key: `github:owner/repo` or `github:owner/repo::subpath`
- Catalog match: normalized `repo_url` + optional `skill_path` / tree subpath
- Monorepo: root and distinct subpaths are **different** identities
- Existing catalog rows are **never** overwritten (id / slug / repo_url preserved)

Protected issues: `714, 3644, 4353, 5214, 5226, 5403, 2028, 2029, 2030, 2850`  
Blocked labels: `blocked:no-github-repo`, `curio:duplicate`, `groot:published`

## Canary flow

```bash
cd /workspace/proskills-ops

# 1) Discover (dry-run default)
python3 operator/scripts/discover.py \
  --catalog /workspace/website-ops/public/skills-catalog.json \
  --limit 25

# 2) Persist candidates locally (still no GitHub mutations)
python3 operator/scripts/discover.py \
  --catalog /workspace/website-ops/public/skills-catalog.json \
  --limit 25 --apply

# 3) Canary plan (dry-run)
python3 operator/scripts/publish_canary.py \
  --catalog /workspace/website-ops/public/skills-catalog.json \
  --top 3 --offline

# 4) Stage catalog append for selected issues
python3 operator/scripts/publish_canary.py \
  --catalog /workspace/website-ops/public/skills-catalog.json \
  --issues 1234,1235 --apply

# 5) Later (separate step): website PR from staged catalog — never merges
python3 operator/scripts/create_pr.py \
  --website-repo /workspace/website-ops \
  --staged-catalog operator/state/artifacts/skills-catalog.staged.json \
  --apply
```

Verification checklist URLs look like:

`https://proskills.md/skills/{category}/{slug}`

## Tests

```bash
cd /workspace/proskills-ops/operator
python3 -m unittest tests.test_publish_path -v
```
