# publish-candidate

Local pipeline skill: prepare branch, catalog edit, and PR. **No merge in pilot.**

## Inputs

- Quality/risk recommendation (promote)
- Target repo name from config `repos` list
- Catalog delta (stub)

## Access

- Python: `scripts/catalog_update.py`, `scripts/create_pr.py`
- Dry-run default; `--apply` only with operator intent
- GitHub deferred until Oracle bridge — no remote in Phase 3

## Steps

1. Re-check approval gates (`merge`/`publish` still blocked in pilot).
2. Plan catalog update (`catalog_update.py --dry-run` by default).
3. Plan branch + PR (`create_pr.py --dry-run` by default).
4. Never call merge APIs; never push without bridge + approval.
5. Emit Oracle-shaped status with `action_required` if human merge needed.

## Validation

- `create_pr.never_merges: true` honored.
- `dry_run: true` unless explicitly applying under policy.

## Output

- Planned or created branch/PR references (stub message in Phase 3)
- Structured JSON `{stage: create_pr|catalog_update, status, dry_run, message, never_merges?}`

## Failure behavior

- Any merge attempt → hard fail / refuse.
- Remote unavailable → stay in dry-run; report deferral.
- Partial apply → reconcile via `scripts/reconcile.py`.

## Approvals

- Merge: required
- Publish: required
- Pilot: prepare only; no auto-merge
