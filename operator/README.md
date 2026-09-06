# ProSkills Operator Workspace

Durable local workspace for **ProSkills.md** funnel ownership: discover → intake → validate → sandbox → score → publish-candidate → maintain → report.

## Principles

- **Python-first**: deterministic pipeline stages are plain Python CLIs (stdlib only in scaffold).
- **AI-for-judgment only**: Grok (or similar) is used when rules are ambiguous, for quality/risk scoring, and for draft community replies — not for routine control flow.
- No credentials in chat, logs, or commits. No remote push from this bootstrap.

## Layout

```
proskills/
  README.md
  config/           # pipeline.example.yaml, policies.yaml
  scripts/          # stage CLIs (discover, intake, validate, …)
  skills/           # local pipeline skill definitions (SKILL.md)
  state/            # runtime DB lives here later (*.sqlite3 gitignored)
  logs/             # run logs (contents gitignored; .gitkeep kept)
  repos/            # local working copies placeholder (.gitkeep only)
```

## Old Curio roles → this workspace

| Old role | New ownership |
|----------|----------------|
| **Scout** | `scripts/discover.py` — discovery + dedupe (Python) |
| **Rocket** | Rules in `config/policies.yaml` + validate; Grok only if ambiguous |
| **Drax** | `scripts/sandbox_test.py` — sandbox checks |
| **Gamora** | Grok quality/risk via `skills/quality-score` |
| **Groot** | `scripts/catalog_update.py` + `create_pr.py` — branch/catalog/PR (Python); never merges in pilot |
| **Mantis** | Grok drafts community replies; **send needs approval** |

## Safety

Approval required for: merge, publish, delete, external messages, permission changes, new connectors, and routines after pilot.

Never store secrets, tokens, private keys, or browser profiles in this tree. See `.gitignore`.

## Status

**Phase 3 — workspace bootstrap.** GitHub access deferred until Oracle bridge. No remotes, no clones, no secrets in this scaffold.

## Oracle event JSON contract

Outbound/inbound event payloads use these fields:

| Field | Description |
|-------|-------------|
| `project` | Project id (e.g. `proskills-md`) |
| `event_id` | Idempotent event identifier |
| `stage` | Pipeline stage name |
| `status` | e.g. `ok`, `error`, `needs_approval`, `skipped` |
| `item` | Subject item / skill / repo ref |
| `summary` | Human-readable summary |
| `evidence_urls` | Related evidence links |
| `action_required` | Whether human/Oracle action is needed |
| `timestamp` | ISO-8601 UTC timestamp |

## Quick start

```bash
cd /workspace/proskills
python3 scripts/intake.py --dry-run
python3 scripts/discover.py --dry-run
python3 scripts/report.py --dry-run
```

Copy `config/pipeline.example.yaml` to a local (gitignored) config when wiring Oracle; keep `dry_run: true` until bridge is live.
