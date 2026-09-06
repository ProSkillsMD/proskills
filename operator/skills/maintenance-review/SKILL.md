# maintenance-review

Local pipeline skill: periodic maintenance — stale state, missing artifacts, drift.

## Inputs

- Pipeline state (when `state/*.sqlite3` exists; not created in scaffold)
- Config pause thresholds and schedules (stubs in `pipeline.example.yaml`)
- Prior stage logs under `logs/` (runtime)

## Access

- Read/repair plan via `scripts/reconcile.py`
- No delete of published artifacts without approval
- No new connectors/routines without approval after pilot

## Steps

1. Scan for stale/missing state and orphan work items.
2. Run `reconcile.py` (dry-run to report, then repair when enabled).
3. Check pause thresholds (error rate, consecutive failures, backlog).
4. Queue follow-ups or pause flags for Oracle.
5. Optionally feed summary into `pipeline-report`.

## Validation

- Reconcile actions are idempotent.
- Destructive cleanup never runs without approval.

## Output

- Drift report + structured reconcile status JSON
- Pause recommendation if thresholds exceeded

## Failure behavior

- DB missing (Phase 3) → ok stub message, no invent data.
- Repair failure → leave item flagged; do not delete.

## Approvals

- Delete: required
- Routines after pilot: required
- Permission changes: required
