# pipeline-report

Local pipeline skill: aggregate counts and status for operators / Oracle.

## Inputs

- Stage outcomes and counts from state DB (when present)
- Config: `project`, `oracle_report.endpoint` placeholder
- Optional date/batch window

## Access

- Read-only reporting via `scripts/report.py`
- May POST to Oracle report endpoint later — **not** in Phase 3 bootstrap
- Redact secrets from any log excerpts

## Steps

1. Collect counts: discovered, intake, validated, sandboxed, scored, PR-open, published.
2. Build Oracle event fields: `project`, `event_id`, `stage=report`, `status`, `item`, `summary`, `evidence_urls`, `action_required`, `timestamp`.
3. Emit local JSON via `report.py`; defer network until bridge.
4. Surface `action_required` when backlog/pause thresholds trip.

## Validation

- Report payload includes all Oracle contract fields when sending.
- `dry_run` does not hit network.

## Output

- Structured JSON with `counts` and message
- Human-readable summary for operators

## Failure behavior

- Missing DB → zero counts + clear message (scaffold behavior).
- Oracle endpoint failure (future) → retain local report; retry with backoff; no credential leakage in errors.

## Approvals

- Report emit: no approval.
- External messages beyond Oracle contract: approval required (Mantis send path).
