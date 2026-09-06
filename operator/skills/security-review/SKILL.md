# security-review

Local pipeline skill: license, manifest, metadata, and safety policy checks (Rocket rules path).

## Inputs

- Candidate item path or metadata blob from intake
- `config/policies.yaml` python-first + safety rules
- Optional evidence URLs from Oracle event

## Access

- Read: candidate files under `repos/` (when populated), policies
- Execute: `scripts/validate.py` (and later sandbox prep)
- No secrets in logs; no credential files may be opened for exfiltration

## Steps

1. Run deterministic license/manifest/metadata validation (`scripts/validate.py`).
2. Flag forbidden patterns (embedded secrets, unexpected permissions, unsigned blobs).
3. If rules are ambiguous → escalate to AI judgment only; do not auto-approve.
4. Record pass/fail with evidence references.

## Validation

- License allow/deny list applied.
- Manifest required fields present.
- No raw secrets detected in candidate payload.

## Output

- Structured JSON from validate stage plus review notes
- `action_required: true` when ambiguous or high risk

## Failure behavior

- Hard policy fail → block progression to publish-candidate.
- Tool/IO error → `status: error`, retryable; do not invent a pass.
- Ambiguous → hold for human/AI judgment; never silent pass.

## Approvals

- Clear rule failures: no auto-override without approval.
- Permission changes / new connectors: always require approval (Phase 9).
