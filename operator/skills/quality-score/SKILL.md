# quality-score

Local pipeline skill: quality and risk scoring (Gamora — AI-for-judgment).

## Inputs

- Validated candidate metadata + security-review result
- Sandbox evidence if available (`scripts/sandbox_test.py` output)
- Scoring rubric (operator-defined; stub in Phase 3)

## Access

- Read: prior stage artifacts, policies
- AI judgment: allowed for quality/risk only (not merge/publish)
- No external send; no credential access

## Steps

1. Confirm prior stages (`validate`, preferably `sandbox_test`) completed.
2. Score quality dimensions (docs, tests, clarity, maintenance signals).
3. Score risk dimensions (supply chain, permissions, blast radius).
4. Produce combined recommendation: promote / hold / reject.
5. If score near threshold → set `action_required: true`.

## Validation

- Scores are numeric or enumerated bands with rationale.
- Recommendation never implies merge/publish without approval gate.

## Output

- Score object + summary suitable for Oracle event `summary` / `evidence_urls`
- Structured status for reporting

## Failure behavior

- Missing prior evidence → hold, do not invent scores.
- AI unavailable → fall back to hold + `action_required: true`.

## Approvals

- Scoring itself: no approval.
- Acting on promote (PR/publish): approval required per policies.
