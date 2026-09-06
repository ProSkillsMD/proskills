# ProSkills Operator — Phase 3B Baseline

Stdlib-only local operator scaffold. No real Oracle contact, no GitHub auth in
scripts by default, no committed secrets. Mock HTTPS / loopback only for
delivery tests. Operator takeover notes below extend the Phase 3B baseline.

## Components

### Scripts (`scripts/`)

| Script | Role |
|--------|------|
| `init_state.py` | Create/migrate SQLite schema; optional state-root dirs |
| `intake.py` | Register event + candidate (idempotent on `event_id`) |
| `validate.py` | Structure/limits checks (no code execution) |
| `static_scan.py` | Text-only pattern scan; writes redacted artifacts under state-root |
| `pipeline.py` | Orchestrate init→intake→validate→static_scan→report with event locks |
| `report.py` | Summarize stage status |
| `purge_artifacts.py` | Retention purge of structured evidence (state-root scoped) |
| `oracle_outbox.py` | Validate + enqueue outbound_events |
| `oracle_delivery.py` | Deliver pending outbox over HTTPS (env credentials only) |
| `health_report.py` | Local DB counts + optional saved inventory JSON; dry-run default; `--apply` writes redacted JSON under state-root/logs; **never** calls network |
| `inventory_github.py` | Static org inventory from known facts + local clone `.github` scan; optional `gh` auth check only with `--use-gh`; prefers `--from-json` / hardcoded facts |
| `testing/mock_oracle_server.py` | Loopback mock helper (tests) |
| `_common.py` | Shared schema, redaction, policies, `StatePaths`, path safety |

### Schema (SQLite)

Tables: `events`, `candidates`, `stage_runs`, `artifacts`, `approvals`, `outbound_events`
(with `attempt_count` / `next_attempt_at` migration).

### Fixtures

- `fixtures/safe-skill/` — clean SKILL.md
- `fixtures/flagged-skill/` — intentional critical text patterns (not executed)
- `fixtures/supervised-6014/` — TEXT-ONLY stub from issue #6014 title/body (no remote skill code)

### Config

- `config/policies.yaml` — limits, retention, AI escalation, oracle.delivery allowlist
- `config/pipeline.example.yaml` — example wiring (dry-run oriented)

### Inventory artifacts

- `inventory/inventory-2026-09-06.json` (also copied to `docs/inventory-2026-09-06.json`)
- Shallow clones (read-only reference): `repos/proskills`, `repos/avenger-initiative`, `repos/skill-claude-code-cli` — **do not execute scripts from clones**

## Commands

Defaults: dry-run (no writes) unless `--apply`. Default `--state-root` is
`/workspace/proskills/state`. `--db` is independent; artifacts/locks/logs never
come from `db.parent`.

```bash
# Schema + state dirs
python3 scripts/init_state.py --state-root /workspace/proskills/state --db /workspace/proskills/state/pipeline.sqlite3 --apply

# Pipeline
python3 scripts/pipeline.py --event-id demo-1 --local-path fixtures/safe-skill --name safe-skill
python3 scripts/pipeline.py --event-id demo-1 --local-path fixtures/safe-skill --name safe-skill \
  --state-root /workspace/proskills/state --db /workspace/proskills/state/pipeline.sqlite3 --apply

# Health / inventory (local only)
python3 scripts/inventory_github.py --apply --out inventory/inventory-2026-09-06.json
python3 scripts/health_report.py --inventory-json inventory/inventory-2026-09-06.json
python3 scripts/health_report.py --inventory-json inventory/inventory-2026-09-06.json \
  --state-root /workspace/proskills/state --db /workspace/proskills/state/pipeline.sqlite3 --apply

# Purge (dry-run default)
python3 scripts/purge_artifacts.py --state-root /workspace/proskills/state --db /workspace/proskills/state/pipeline.sqlite3
python3 scripts/purge_artifacts.py --state-root /workspace/proskills/state --db /workspace/proskills/state/pipeline.sqlite3 --apply

# Outbox / delivery (credentials from env only; never commit them; do NOT hit real Oracle)
python3 scripts/oracle_outbox.py --help
python3 scripts/oracle_delivery.py --help   # --insecure-for-tests requires PROSKILLS_ENV=test

# Tests (always use temp state-root inside tests)
cd /workspace/proskills && PROSKILLS_ENV=test python3 -m unittest discover -s tests -v
```

## Policy defaults

- Dry-run by default; `--apply` required for persistence / delivery POSTs
- Limits: 200 files / 5 MiB per candidate; batch_size 10; model_review_threshold 0.7
- Artifact retention: 30 days structured evidence; never retain candidate secrets
- AI escalation: never override deterministic critical findings
- Oracle delivery allowlist: `localhost`, `127.0.0.1`; HTTPS required; no redirects
- Owner-only perms where supported: directories `0o700`, files `0o600`

## Security invariants

1. **Explicit `--state-root`**: artifacts, locks, and logs bind only to `--state-root`, never inferred from `--db` alone.
2. **Symlink rejection**: symlinked state roots are rejected; purge/walk do not follow escaping symlinks; never unlink a realpath outside state-root.
3. **Path containment**: writes under state-root must resolve inside the resolved root (no parent escapes).
4. **Owner-only permissions**: state dirs `0700`, state files `0600` after create (Linux).
5. **Secret redaction**: logs/artifacts/stdout must not contain raw tokens/keys; env-only credentials for delivery.
6. **`--insecure-for-tests`**: hard-error unless `PROSKILLS_ENV=test`; even then, non-loopback destinations are rejected.
7. **No candidate code execution**: static scan is text-only.
8. **Tests isolate state**: unittest apply paths use unique temporary state roots under `/tmp`, not the shared project `state/`.

## Org inventory summary (2026-09-06)

| Repo | Role | Notes |
|------|------|-------|
| `ProSkillsMD/proskills` | Catalog | ~3184 open issues (submission backlog); structure: `listings/`, `skills/`, `reviews/`, `pending/`, `hosted/`, `index.json`, `_stats.json`, `.github/`; many branches including `groot/publish/*`; **no releases** |
| `ProSkillsMD/avenger-initiative` | Backup skill | Shallow-cloned for reference; no `.github/workflows` |
| `ProSkillsMD/skill-claude-code-cli` | Skill | Shallow-cloned for reference; no `.github/workflows` |

**Local `.github` scan (clones):**

- `proskills`: `.github/ISSUE_TEMPLATE/` only (`feature-request.yml`, `submit-skill.yml`, `report-skill.yml`); **no `workflows/` directory**
- `avenger-initiative` / `skill-claude-code-cli`: no `.github` directory present in shallow clone

**Curio labels observed on issues:** `curio:routed-to-rocket`, `curio:routed-to-drax`, `curio:routed-to-gamora`, `rocket-pass`, `drax-pass`, `gamora-conditional`, `submission`, `auto-discovered` (also seen: `curio:publishing` on #6014).

**Catalog stats (from clone `_stats.json` / `index.json`):** ~436–438 skills indexed; average score ~7.9; last_scan ~2026-03-17.

## Role consolidation map (Avengers → scripts)

| Legacy role | Responsibility | Operator mapping |
|-------------|----------------|------------------|
| **Scout** | Discover / auto-submit candidates | `scripts/discover.py` + issue labels `auto-discovered` / `submission`; inventory via `inventory_github.py` |
| **Rocket** | Fast screen / rule pass | `scripts/validate.py` + `scripts/static_scan.py`; AI only for ambiguous rule resolution (`policies.yaml`) |
| **Drax** | Literal / security checks | `scripts/static_scan.py` + `skills/security-review`; labels `drax-pass` |
| **Gamora** | Quality / risk judgment | `skills/quality-score` (AI judgment only); labels `gamora-conditional` |
| **Groot** | Grow / publish branches | `scripts/catalog_update.py`, `scripts/create_pr.py` (dry-run default; never merges); branches `groot/publish/*` are reference only — **no auto-merge** |
| **Mantis** | Community replies | Draft-only skill path; external messages require approval (`approval_gates.external_messages`) |

Pipeline spine: `pipeline.py` = init → intake → validate → static_scan → report; outbox = `oracle_outbox.py` (queue only); delivery = `oracle_delivery.py` (mock/loopback in tests).

## Decision log

1. **PAT identity:** GitHub access used for operator inventory is personal **Asif2BD**, not a dedicated service account. Do not treat it as a bot identity; rotate / migrate to GitHub App or machine user before unattended routines.
2. **GitHub App inventory:** Pending API enumeration (curiobot4proskills observed as issue author on #6014). Do not modify GitHub App settings in this phase.
3. **Backlog:** ~**3184** open submission issues on `ProSkillsMD/proskills` — autonomy must batch with limits (`batch_size: 10`) and approval gates.
4. **Actions workflows:** No `.github/workflows` directory found in local shallow clones (initially / as of 2026-09-06 scan). Issue templates only on catalog repo.
5. **Autonomy gated:** Merge, publish, delete, permission changes, public announcements, production changes, and **real Oracle delivery** require explicit human approval. Pilot keeps create_pr / catalog_update dry-run default and `never_merges: true`.
6. **Supervised fixture policy:** For issue-driven dry-runs, extract TEXT ONLY into `fixtures/supervised-*`; prefer `fixtures/safe-skill` as `--local-path` so no remote submission code is executed on the operator VM.
7. **No commit / no push to main** in this takeover pass; local workspace updates preferred.

## Proposed routines (drafts only — not live)

Markdown drafts under `automations/drafts/` (and related skill notes). **Do not** create live Grok Bot / Cursor routines that fire GitHub listeners until the user approval card is granted.

| Draft | Intent | Trigger (proposed) | Guardrails |
|-------|--------|--------------------|------------|
| `routine-health-report.md` | Periodic local health JSON | Schedule / manual | Local DB + inventory file only; no network |
| `routine-inventory-refresh.md` | Refresh static inventory from clones + known facts | Manual / weekly | No bulk issue fetch without approval; no tokens in output |
| `routine-supervised-intake.md` | Dry-run pipeline on labeled submissions using fixtures | Manual / issue label | TEXT fixtures only; temp state-root; no publish |
| `routine-oracle-outbox-drain-mock.md` | Drain outbox to mock Oracle | Test env only | `PROSKILLS_ENV=test` + loopback; never real Oracle |

## Next autonomy enable steps (approval required)

1. Confirm service identity (GitHub App install inventory + least-privilege token) — replace personal PAT for unattended work.
2. User approval card for any live routine that listens to GitHub issues/PRs.
3. Define batch triage policy for the 3184-open backlog (label queues: rocket → drax → gamora).
4. Enable mock-only outbox drain in CI; keep real Oracle endpoint blocked until allowlist + credentials reviewed.
5. Optionally add `.github/workflows` for CI of **this operator tree** only (not catalog publish) after review.
6. Re-approve `routines_after_pilot` gate before leaving pilot mode (`config/policies.yaml`).

## Known limitations

- YAML loader is a restricted subset (no anchors/aliases/multiline blocks).
- Delivery uses stdlib urllib; connect/read timeouts are summed into one timeout.
- Mock TLS uses `--insecure-for-tests` (test env + loopback only); production must use real CA verification.
- GitHub live enumeration / real Oracle / live routines remain intentionally gated.
- Windows ACL semantics are not modeled; chmod is best-effort.

## Non-goals / prohibited in this tree

- Real Oracle endpoints, production tokens, private URLs in docs or logs
- Committing secrets, browser profiles, or live credentials
- Connecting external services from baseline documentation examples
- Merge / publish / delete / permission changes / public announcements without approval
- Executing untrusted submitted skill code on the operator VM
