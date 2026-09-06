# intake-review

Local pipeline skill: review and accept inbound Oracle/operator events into the funnel.

## Inputs

- Oracle event JSON: `project`, `event_id`, `stage`, `status`, `item`, `summary`, `evidence_urls`, `action_required`, `timestamp`
- Optional: local config from `config/pipeline.example.yaml` (or operator copy)

## Access

- Read: inbound event payload, `config/policies.yaml`
- Write (runtime only): `state/` when DB exists — **not** in Phase 3 scaffold
- No credentials; no network publish

## Steps

1. Validate required Oracle contract fields are present.
2. Check idempotency: if `event_id` already processed, skip or ack.
3. Normalize `item` / `summary` for downstream stages.
4. Enqueue for `validate` / `security-review` as policies require.
5. Emit structured status JSON via `scripts/intake.py`.

## Validation

- All contract fields parseable; `event_id` non-empty for persist paths.
- `project` matches configured project (`proskills-md`).

## Output

- Structured JSON: `{stage: intake, status, dry_run, message, event_id?}`
- Queued work item reference (stub until state DB)

## Failure behavior

- Malformed event → `status: error`, do not persist.
- Duplicate `event_id` → `status: skipped` (or ack), no double-process.
- Missing config → fail closed with clear message.

## Approvals

- Intake itself: no approval in pilot (read/normalize only).
- Downstream publish/merge/external send: gated per `config/policies.yaml`.
