# DRAFT routine: mock Oracle outbox drain

**Status:** draft only — NEVER point at real Oracle without explicit approval.

## Intent

Enqueue completion events locally (`oracle_outbox.py`) and optionally deliver to
loopback mock server under `PROSKILLS_ENV=test`.

## Proposed command

```bash
# enqueue only (temp db)
python3 scripts/oracle_outbox.py \
  --event-id supervised-6014 \
  --stage report --status completed \
  --item openclaw-skills \
  --summary "Supervised local pipeline completion (fixture only; no publish)" \
  --evidence-url https://github.com/ProSkillsMD/proskills/issues/6014 \
  --no-action-required \
  --db "$TMP/pipeline.sqlite3" --apply

# delivery: tests / mock only — see scripts/oracle_delivery.py --help
# PROSKILLS_ENV=test required for --insecure-for-tests
```

## Guardrails

- Allowlist hosts only (`localhost`, `127.0.0.1`).
- No production credentials in workspace.
- Do not contact real Oracle endpoint.
